"""动态权重校准器 — IC 分析 + 单调性测试 → 数据驱动的因子权重

触发条件:
  - 月边界: 每月自动校准
  - 状态变化: L1 市场状态切换时即时校准
  - 手动触发: CLI 调用

R4: 形态乘数 (pattern_mult) 不参与动态权重校准，保持人工规则。
"""

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from core.logger import get_logger

logger = get_logger("factors.weights_calibrator")


@dataclass(frozen=True)
class CalibrationResult:
    weights: dict[str, float]
    ic_report: dict[str, float] = field(default_factory=dict)
    monotonicity_report: dict[str, float] = field(default_factory=dict)
    calibrated_at: str = ""
    trigger: str = ""  # "monthly" / "state_change" / "manual"


def calibrate_weights(
    store,  # DataStore
    lookback_months: int = 12,
    market_state: str = "volatile",
    n_stocks: int = 500,
) -> CalibrationResult:
    """运行 IC 分析 + 单调性测试，产出校准后权重。

    步骤:
      1. IC 分析 — 计算各因子与未来收益的 Spearman 相关系数
      2. 单调性测试 — 验证因子值分组收益单调性
      3. 合成权重 — 按有效性分配，归一化到 100，排除形态相关因子

    Args:
        store: DataStore 实例
        lookback_months: 回溯月数
        market_state: 当前 L1 市场状态
        n_stocks: 校准用股票数

    Returns:
        CalibrationResult 含校准后权重
    """
    from factors.multi_factor import DEFAULT_WEIGHTS, _FACTOR_COLUMNS

    calibrated_at = datetime.now().isoformat()

    try:
        ic_scores = _run_ic_analysis(store, lookback_months, n_stocks)
    except Exception as e:
        logger.warning(f"IC 分析失败，使用默认权重: {e}")
        return CalibrationResult(
            weights=DEFAULT_WEIGHTS.copy(),
            calibrated_at=calibrated_at,
            trigger="fallback",
        )

    try:
        mono_scores = _run_monotonicity_check(store, lookback_months, n_stocks)
    except Exception as e:
        logger.warning(f"单调性测试失败，仅用 IC: {e}")
        mono_scores = {f: 0.5 for f in _FACTOR_COLUMNS}

    # 合成有效性得分: IC × 0.6 + 单调性 × 0.4
    effectiveness: dict[str, float] = {}
    for factor in _FACTOR_COLUMNS:
        ic = ic_scores.get(factor, 0.0)
        mono = mono_scores.get(factor, 0.5)
        effectiveness[factor] = abs(ic) * 0.6 + mono * 0.4

    # 排除无效因子（得分为负或几乎为零）
    for f in list(effectiveness):
        if effectiveness[f] <= 0.01:
            effectiveness[f] = 0.01  # 保底小权重

    # 按有效性比例分配权重，总和 = 100
    total = sum(effectiveness.values())
    calibrated = {f: round(100 * v / total, 2) for f, v in effectiveness.items()}

    # 归一化确保精确 100
    norm_total = sum(calibrated.values())
    if norm_total != 100:
        scale = 100.0 / norm_total
        calibrated = {k: round(v * scale, 2) for k, v in calibrated.items()}

    logger.info(f"权重校准完成: trigger=monthly, state={market_state}, "
                f"top3={sorted(calibrated.items(), key=lambda x: x[1], reverse=True)[:3]}")

    return CalibrationResult(
        weights=calibrated,
        ic_report=ic_scores,
        monotonicity_report=mono_scores,
        calibrated_at=calibrated_at,
        trigger="monthly",
    )


def _run_ic_analysis(store, lookback_months: int, n_stocks: int) -> dict[str, float]:
    """运行 IC 分析，返回各因子的平均 |IC| 值。

    直接调用 scripts/validate_factors.py 的 step1_factor_ic，
    汇总多个月的 IC 结果。
    """
    from scripts.validate_factors import step1_factor_ic
    from factors.multi_factor import _FACTOR_COLUMNS

    # step1_factor_ic 是 CLI 脚本，这里做简化版
    symbols = store.stock_names[:n_stocks]

    all_month_ends = set()
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is not None and len(daily) >= 252:
            ends = [e for e in daily.resample("ME").groups.keys()]
            all_month_ends.update(ends)
    all_month_ends = sorted(all_month_ends)

    # 取最近 lookback_months 个月
    recent_ends = all_month_ends[-lookback_months:] if len(all_month_ends) > lookback_months else all_month_ends

    if not recent_ends:
        return {f: 0.0 for f in _FACTOR_COLUMNS}

    factor_ics: dict[str, list[float]] = {f: [] for f in _FACTOR_COLUMNS}

    for month_end in recent_ends:
        factors_list = []
        forward_rets = []

        for sym in symbols:
            daily = store.get_daily(sym)
            if daily is None or len(daily) < 60:
                continue
            daily_cut = daily.loc[:month_end]
            if len(daily_cut) < 60:
                continue

            # 前向收益
            future = daily.loc[month_end:].iloc[1:22] if month_end in daily.index else pd.DataFrame()
            if len(future) < 5:
                continue
            fwd_ret = float(future["close"].iloc[-1] / future["close"].iloc[0] - 1)

            # 原始因子
            from factors.multi_factor import compute_raw_factors
            raw = compute_raw_factors(sym, daily_cut)
            if raw is None:
                continue

            factors_list.append(raw)
            forward_rets.append(fwd_ret)

        if len(factors_list) < 30:
            continue

        df = pd.DataFrame(factors_list).set_index("symbol")
        for f in _FACTOR_COLUMNS:
            if f not in df.columns:
                continue
            valid = df[[f]].copy()
            valid["fwd_ret"] = forward_rets[: len(valid)]
            valid = valid.dropna()
            if len(valid) < 20:
                continue
            from scipy.stats import spearmanr
            ic, _ = spearmanr(valid[f], valid["fwd_ret"])
            factor_ics[f].append(ic if not np.isnan(ic) else 0.0)

    # 平均 |IC|
    return {f: float(np.mean([abs(v) for v in vals])) if vals else 0.0
            for f, vals in factor_ics.items()}


def _run_monotonicity_check(store, lookback_months: int, n_stocks: int) -> dict[str, float]:
    """运行单调性测试，返回各因子的单调性得分 (0-1)。"""
    from factors.multi_factor import _FACTOR_COLUMNS

    symbols = store.stock_names[:n_stocks]
    all_month_ends = set()
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is not None and len(daily) >= 252:
            ends = [e for e in daily.resample("ME").groups.keys()]
            all_month_ends.update(ends)
    all_month_ends = sorted(all_month_ends)

    recent_ends = all_month_ends[-lookback_months:] if len(all_month_ends) > lookback_months else all_month_ends
    if not recent_ends:
        return {f: 0.5 for f in _FACTOR_COLUMNS}

    factor_mono: dict[str, list[float]] = {f: [] for f in _FACTOR_COLUMNS}

    for month_end in recent_ends:
        factors_list = []
        fwd_rets = []
        for sym in symbols:
            daily = store.get_daily(sym)
            if daily is None or len(daily) < 60:
                continue
            daily_cut = daily.loc[:month_end]
            if len(daily_cut) < 60:
                continue
            future = daily.loc[month_end:].iloc[1:22] if month_end in daily.index else pd.DataFrame()
            if len(future) < 5:
                continue
            fwd_ret = float(future["close"].iloc[-1] / future["close"].iloc[0] - 1)

            from factors.multi_factor import compute_raw_factors
            raw = compute_raw_factors(sym, daily_cut)
            if raw is None:
                continue
            factors_list.append(raw)
            fwd_rets.append(fwd_ret)

        if len(factors_list) < 50:
            continue

        df = pd.DataFrame(factors_list).set_index("symbol")

        for f in _FACTOR_COLUMNS:
            if f not in df.columns:
                continue
            valid = df[[f]].copy()
            valid["fwd_ret"] = fwd_rets[: len(valid)]
            valid = valid.dropna()
            if len(valid) < 50:
                continue

            # 分5组，检查单调性
            valid["q"] = pd.qcut(valid[f].rank(method="first"), 5, labels=False)
            group_rets = valid.groupby("q")["fwd_ret"].mean()
            mono_count = sum(
                1 for i in range(len(group_rets) - 1)
                if group_rets.iloc[i + 1] >= group_rets.iloc[i]
            )
            factor_mono[f].append(mono_count / 4.0)

    return {f: float(np.mean(vals)) if vals else 0.5
            for f, vals in factor_mono.items()}
