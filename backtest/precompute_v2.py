"""两阶段 L3 预计算 V2 — 全历史时间序列 + 按月截面提取

Phase A: 每只股票计算全历史因子和门信号时间序列 (一次性 O(stocks × data_len))
Phase B: 按月提取月未截面值 → 标准化 → 评分 → 过门    (快速 O(stocks × months))

相比 V1 提速 50-100x（避免每月重复计算 MACD/分型/背驰）。
"""

from pathlib import Path

import pandas as pd
import numpy as np
from tqdm import tqdm

from data.store import DataStore
from core.logger import get_logger

logger = get_logger("backtest.precompute_v2")

# 因子列表（与 multi_factor._FACTOR_COLUMNS 一致）
FACTOR_COLS = [
    "trend_macd_5dim", "momentum_12_1",
    "reversal_20", "oversold_60", "weekly_deviation",
    "bottom_divergence", "volume_reversal", "bottom_fractal",
    "low_volatility", "turnover_stability", "close_position",
]


def compute_stock_ts(sym: str, store: DataStore) -> dict | None:
    """为单只股票计算全历史因子时间序列 + 门信号。

    返回:
        dict 含 factor DataFrames (daily index) 和 gate Series，
        或 None 表示数据不足。
    """
    from factors.macd import calc_macd
    from factors.precompute import precompute_stock_all_factors
    from factors.chanlun.fractal import identify_fractals

    daily = store.get_daily(sym)
    if daily is None or len(daily) < 252:
        return None

    weekly = store.get_weekly(sym)
    monthly = store.get_monthly(sym)

    # 基础因子（已向量化）
    factors_df = precompute_stock_all_factors(daily, weekly, monthly)
    if factors_df.empty:
        return None

    # ── MACD 时间序列 ──
    daily_dif, daily_dea, daily_hist = calc_macd(daily["close"])

    weekly_dif = weekly_dea = weekly_hist = None
    if weekly is not None and len(weekly) >= 10:
        weekly_dif, weekly_dea, weekly_hist = calc_macd(weekly["close"])

    monthly_dif = monthly_dea = monthly_hist = None
    if monthly is not None and len(monthly) >= 6:
        monthly_dif, monthly_dea, monthly_hist = calc_macd(monthly["close"])

    # ── Gate 信号时间序列 ──
    gates = _compute_gate_ts(
        daily, daily_dif, daily_dea, daily_hist,
        weekly, weekly_dif, weekly_dea, weekly_hist,
        monthly, monthly_dif, monthly_dea, monthly_hist,
    )
    if gates is None:
        return None

    return {
        "symbol": sym,
        "factors_df": factors_df,
        **gates,
    }


def _compute_gate_ts(
    daily: pd.DataFrame,
    daily_dif: pd.Series, daily_dea: pd.Series, daily_hist: pd.Series,
    weekly: pd.DataFrame | None,
    weekly_dif, weekly_dea, weekly_hist,
    monthly: pd.DataFrame | None,
    monthly_dif, monthly_dea, monthly_hist,
) -> dict | None:
    """计算门信号全时间序列（对齐到日线索引）。"""
    from factors.chanlun.fractal import identify_fractals
    from factors.chanlun.divergence import detect_top_divergence, detect_bottom_divergence

    idx = daily.index
    n = len(idx)
    result = {}

    # B1: 周线 MACD 多头 -> 前向填充到日线
    if weekly_dif is not None and weekly_hist is not None:
        b1_weekly = (
            (weekly_dif > weekly_dea) & (weekly_hist > 0)
        ).astype(bool)
        result["b1_weekly_macd_bull"] = (
            b1_weekly.reindex(idx, method="ffill").fillna(False)
        )
    else:
        result["b1_weekly_macd_bull"] = pd.Series(False, index=idx)

    # B2: 月线 MACD 多头
    if monthly_dif is not None and monthly_dea is not None:
        b2_golden = _ts_golden_cross(monthly_dif, monthly_dea, window=6)
        b2_above_zero = monthly_dif > 0
        b2_turning_up = _ts_dif_turning_up(monthly_dif)
        b2_monthly = b2_golden | b2_above_zero | b2_turning_up
        result["b2_monthly_macd_bull"] = (
            b2_monthly.reindex(idx, method="ffill").fillna(False)
        )
    else:
        result["b2_monthly_macd_bull"] = pd.Series(False, index=idx)

    # 分型（日线和周线，各计算一次）
    daily_fractals = identify_fractals(daily.copy())
    daily_top_frac = daily_fractals["top_fractal"] if "top_fractal" in daily_fractals.columns else pd.Series(False, index=idx)
    daily_bottom_frac = daily_fractals["bottom_fractal"] if "bottom_fractal" in daily_fractals.columns else pd.Series(False, index=idx)

    if weekly is not None and len(weekly) >= 20:
        weekly_fractals = identify_fractals(weekly.copy())
        weekly_top_frac = weekly_fractals["top_fractal"] if "top_fractal" in weekly_fractals.columns else pd.Series(False, index=weekly.index)
    else:
        weekly_top_frac = pd.Series(False, index=idx[:1])

    # T1: 日线顶背驰
    daily_with_frac = daily.copy()
    daily_with_frac["top_fractal"] = daily_top_frac
    daily_with_frac["bottom_fractal"] = daily_bottom_frac
    result["t1_daily_top_div"] = detect_top_divergence(daily_with_frac, daily_hist)

    # T1: 周线顶背驰
    if weekly is not None and len(weekly) >= 20 and weekly_hist is not None:
        weekly_with_frac = weekly.copy()
        weekly_with_frac["top_fractal"] = weekly_top_frac
        weekly_t1 = detect_top_divergence(weekly_with_frac, weekly_hist)
        result["t1_weekly_top_div"] = (
            weekly_t1.reindex(idx, method="ffill").fillna(False)
        )
    else:
        result["t1_weekly_top_div"] = pd.Series(False, index=idx)

    # T2: 周线死叉近8周
    if weekly_dif is not None and weekly_dea is not None:
        death_cross = (weekly_dif < weekly_dea) & (weekly_dif.shift(1) >= weekly_dea.shift(1))
        t2_weekly = death_cross.rolling(8, min_periods=1).max().fillna(0).astype(bool)
        result["t2_weekly_death_cross"] = (
            t2_weekly.reindex(idx, method="ffill").fillna(False)
        )
    else:
        result["t2_weekly_death_cross"] = pd.Series(False, index=idx)

    # T3: 日线死叉 or DIF<0
    daily_death = (daily_dif < daily_dea) & (daily_dif.shift(1) >= daily_dea.shift(1))
    daily_death_5d = daily_death.rolling(5, min_periods=1).max().fillna(0).astype(bool)
    daily_dif_below = daily_dif < 0
    result["t3_daily_death_or_below"] = daily_death_5d | daily_dif_below

    # bearish_weighted
    result["bearish_weighted"] = (
        result["t1_daily_top_div"].astype(int) * 2 +
        result["t1_weekly_top_div"].astype(int) * 3 +
        result["t2_weekly_death_cross"].astype(int) * 2 +
        result["t3_daily_death_or_below"].astype(int) * 1
    )

    # 硬约束
    result["listing_days"] = pd.Series(n, index=idx)
    if "amount" in daily.columns:
        result["daily_amount"] = daily["amount"].rolling(20).mean()
    elif "volume" in daily.columns:
        result["daily_amount"] = daily["volume"].rolling(20).mean()
    else:
        result["daily_amount"] = pd.Series(1e6, index=idx)

    return result


def _ts_golden_cross(dif: pd.Series, dea: pd.Series, window: int = 6) -> pd.Series:
    """检测金叉信号：DIF 从下方穿越 DEA，在 window 内有效。"""
    cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    return cross.rolling(window, min_periods=1).max().fillna(0).astype(bool)


def _ts_dif_turning_up(dif: pd.Series) -> pd.Series:
    """检测 DIF 拐头向上：当前 DIF > 前值 DIF。"""
    return dif > dif.shift(1)


def precompute_all_stocks_v2(
    store: DataStore,
    hs300_daily: pd.DataFrame | None = None,
    start: str | None = None,
    end: str | None = None,
    cache_dir: Path | None = None,
) -> pd.DataFrame:
    """两阶段 L3 预计算。

    阶段A: 逐只计算全历史因子时间序列 (可缓存到 parquet)
    阶段B: 按月提取月未截面值 → 标准化 → 评分 → 过门
    """
    from factors.multi_factor import (
        process_cross_section, aggregate_scores,
        apply_hard_filters, check_market_trend,
    )

    symbols = store.stock_names
    logger.info(f"V2 预计算 L3: {len(symbols)} 只股票")

    # ── 收集月末时点 ──
    all_month_ends: set[pd.Timestamp] = set()
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is not None and len(daily) >= 200:
            all_month_ends.update(daily.resample("ME").groups.keys())
    all_month_ends = sorted(all_month_ends)

    if start is not None:
        pre_start = pd.Timestamp(start) - pd.DateOffset(months=6)
        all_month_ends = [m for m in all_month_ends if m >= pre_start]
    if end is not None:
        all_month_ends = [m for m in all_month_ends if m <= pd.Timestamp(end)]

    if not all_month_ends:
        logger.warning("无符合条件的月末时点")
        return pd.DataFrame()

    logger.info(f"月末时点: {len(all_month_ends)} 个")

    # ── 阶段A: 逐只全历史时间序列 ──
    cache_file = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"ts_factors_{len(symbols)}.parquet"

    if cache_file and cache_file.exists():
        logger.info(f"加载缓存: {cache_file}")
        ts_factors = pd.read_parquet(cache_file)
    else:
        all_ts = []
        for sym in tqdm(symbols, desc="阶段A: 时间序列"):
            ts = compute_stock_ts(sym, store)
            if ts is not None:
                # 展平为长格式: symbol, date, factor_1, ..., factor_n, gate_1, ...
                factors_df = ts["factors_df"].copy()
                factors_df["symbol"] = sym
                for gate_key in ["b1_weekly_macd_bull", "b2_monthly_macd_bull",
                                  "t1_daily_top_div", "t1_weekly_top_div",
                                  "t2_weekly_death_cross", "t3_daily_death_or_below",
                                  "bearish_weighted", "listing_days", "daily_amount"]:
                    if gate_key in ts:
                        factors_df[gate_key] = ts[gate_key]
                all_ts.append(factors_df.reset_index())

        if not all_ts:
            logger.error("阶段A无数据")
            return pd.DataFrame()

        ts_factors = pd.concat(all_ts, ignore_index=True)
        ts_factors = ts_factors.rename(columns={"index": "date"})
        if cache_file:
            ts_factors.to_parquet(cache_file, index=False)
            logger.info(f"缓存保存: {cache_file}")

    logger.info(f"阶段A完成: {ts_factors['symbol'].nunique()} 只股票, {len(ts_factors)} 条日记录")

    # ── 阶段B: 按月提取截面 ──
    all_rows = []

    for month_end in tqdm(all_month_ends, desc="阶段B: 月截面"):
        month_str = month_end.strftime("%Y-%m")

        # 大盘过滤
        alpha_mult = 1.0
        if hs300_daily is not None:
            alpha_mult = check_market_trend(hs300_daily, month_end)

        # 提取月末最后一天的因子值
        month_mask = ts_factors["date"] <= month_end
        # 对每只股票取最后一天
        last_day = (
            ts_factors[month_mask]
            .groupby("symbol")
            .last()
            .reset_index()
        )

        if last_day.empty:
            continue

        # 硬约束
        if "listing_days" in last_day.columns:
            last_day = last_day[last_day["listing_days"] >= 252]
        if "daily_amount" in last_day.columns:
            last_day = last_day[last_day["daily_amount"] >= 1e5]
        if "is_st" in last_day.columns:
            last_day = last_day[~last_day["is_st"]]

        if last_day.empty:
            continue

        # 截面标准化（仅因子列）
        factor_cols_avail = [c for c in FACTOR_COLS if c in last_day.columns]
        if len(factor_cols_avail) < 3:
            continue

        factor_data = last_day[["symbol"] + factor_cols_avail].copy()
        processed = process_cross_section(factor_data.to_dict("records"))
        if processed.empty:
            continue

        scores = aggregate_scores(processed, alpha_multiplier=alpha_mult)

        # 门检查
        for _, score_row in scores.iterrows():
            sym = score_row.name
            sym_data = last_day[last_day["symbol"] == sym]
            if sym_data.empty:
                continue

            row = sym_data.iloc[0]
            b1 = bool(row.get("b1_weekly_macd_bull", False))
            b2 = bool(row.get("b2_monthly_macd_bull", False))
            bullish = (b1 + b2) >= 2  # 3取2 (B3=资金面=True)

            bearish = int(row.get("bearish_weighted", 0))
            passed = bullish and bearish < 5

            classification = "strong"
            if not passed:
                classification = "rejected"

            all_rows.append({
                "symbol": sym, "month": month_str,
                "passed": passed, "score": float(score_row["score"]),
                "classification": classification,
            })

    result = pd.DataFrame(all_rows)
    if len(result) > 0:
        logger.info(f"阶段B完成: {len(result)} 条, 通过率 {result['passed'].mean():.1%}")
    return result
