"""第二层门卫：ETF分类指数评估（周线）— 统一四维度评分"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from factors.macd import (
    calc_macd, is_golden_cross, is_death_cross,
    is_dif_above_zero, is_dif_turning_up,
)
from factors.chanlun.divergence import (
    check_weekly_top_divergence, check_monthly_bottom_divergence,
    check_weekly_bottom_divergence,
)
from factors.volume import score_volume_three_part
from gate.fund_fallback import assess_fund_for_layer
from gate.threshold import default_top_k
from core.logger import get_logger

logger = get_logger("gate.layer2")

# 看空信号加权阈值
BEARISH_SIGNAL_WEIGHTS = {
    "weekly_top_divergence": 3,
    "weekly_death_cross": 2,
    "daily_death_cross": 1,
    "daily_dif_below_zero": 0,  # 状态描述，非战术信号；弱市中永远为True会虚增拦截
}
BEARISH_INTERCEPT_THRESHOLD = 4

# 动态看空拦截阈值：偏弱市场容忍度更高，牛市更警惕反转
_BEARISH_THRESHOLD_MAP = {"bull": 3, "volatile": 4, "weak": 5, "bear": 999}
# 动态 Gate bullish 门槛：偏弱市场放宽，牛市收紧
_GATE_THRESHOLD_MAP = {"bull": 3, "volatile": 2, "weak": 1, "bear": 999}


@dataclass
class SectorVerdict:
    """单个ETF分类指数评估结果"""
    etf_name: str
    etf_code: str
    passed_gate: bool  # 通过硬门槛
    gate_details: dict = field(default_factory=dict)
    score: float = 0.0  # 过门后评分 (0-100)
    fund_confidence: float = 1.0
    fund_level: str = "L1"
    fund_note: str = ""
    bullish_score: int = 0   # 看多条件命中数 (含Alpha/资金)
    bearish_score: int = 0   # 顶部信号命中数 (0-3)
    risk_warning: bool = False  # 1个顶部信号 → 风险提示
    pattern_bonus: float = 0.0  # 形态修正分 (±15)
    pattern_filtered: bool = False  # 是否因形态不佳被过滤


@dataclass
class Layer2Result:
    """第二层综合结果"""
    candidate_sectors: list[SectorVerdict]  # 过门的候选指数
    strong_sectors: list[SectorVerdict]  # 最终强势指数（TopK）
    passed: bool  # 是否有强势指数
    details: list[str] = field(default_factory=list)
    risk_warning: bool = False  # 任一候选指数出现1个顶部信号


def assess_single_sector(
    etf_name: str,
    etf_code: str,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame | None = None,
    daily_df: pd.DataFrame | None = None,
    benchmark_close: pd.Series | None = None,
    fund_data: dict | None = None,
    score_weights: dict[str, float] | None = None,
    cross_returns: dict[str, dict[str, float]] | None = None,
    gate_threshold: int = 2,
    bearish_threshold: int = 4,
) -> SectorVerdict:
    """
    评估单个ETF分类指数 — 统一四维度评分。

    Gate: 2-of-5 底部共振 + 看空加权拦截 (≥4拦截)
    评分: Trend(30) + Alpha(25) + Volume(25) + Fund(20) = 100

    cross_returns: {etf_name: {"1w": float, "4w": float, "13w": float}}
        用于横截面Alpha百分位排名，None时退化为基准比较
    """
    gate = {}
    weekly_dif, weekly_dea, weekly_hist = calc_macd(weekly_df["close"])
    monthly_dif, monthly_dea, monthly_hist = (calc_macd(monthly_df["close"])
                                               if monthly_df is not None
                                               else (None, None, None))

    # ═══════════════════════════════════════════
    # Gate: 底部共振条件（买入维度，5取2）
    # ═══════════════════════════════════════════
    # B1: 周线MACD金叉状态
    dif_above_dea = bool((weekly_dif > weekly_dea).iloc[-1])
    hist_positive = bool((weekly_hist > 0).iloc[-1])
    b1 = dif_above_dea and hist_positive
    gate["周线MACD_金叉状态"] = b1

    # B2: 月线MACD（无月线数据时不给免费分）
    b2 = False
    if monthly_dif is not None and monthly_dea is not None:
        mg = bool(is_golden_cross(monthly_dif, monthly_dea).tail(4).any())
        md0 = bool(is_dif_above_zero(monthly_dif).iloc[-1])
        mdt = bool(is_dif_turning_up(monthly_dif).iloc[-1])
        monthly_bottom_div = check_monthly_bottom_divergence(monthly_df, monthly_dif)
        b2_conditions = [mg, md0, mdt, monthly_bottom_div]
        b2 = sum(b2_conditions) >= 2  # 至少满足2项
        gate["月线MACD_金叉"] = mg
        gate["月线MACD_零轴上"] = md0
        gate["月线MACD_拐头"] = mdt
        gate["月线MACD_底背离"] = monthly_bottom_div
    else:
        gate["月线MACD"] = "数据缺失(默认通过)"

    # B3: 周线底背驰
    b3 = check_weekly_bottom_divergence(weekly_df, weekly_hist)
    gate["周线底背驰"] = b3

    # B4: 跑赢基准 (保留原始逻辑作为Gate条件)
    b4 = False
    if benchmark_close is not None and len(benchmark_close) > 4:
        stock_ret = weekly_df["close"].iloc[-1] / weekly_df["close"].iloc[-4] - 1
        bench_ret = benchmark_close.iloc[-1] / benchmark_close.iloc[-4] - 1
        b4 = stock_ret > bench_ret
    else:
        b4 = False  # 无基准数据时不默认通过
    gate["跑赢沪深300"] = b4

    # B5: 资金面 (Gate判定用)
    fund_result = _assess_sector_fund(fund_data, weekly_df)
    b5 = fund_result["fund_pass"]
    gate["资金面"] = f"{fund_result['fund_level']} {fund_result['fund_note']}"

    bullish_score = sum([b1, b2, b3, b4, b5])
    gate["底部得分"] = bullish_score

    # ═══════════════════════════════════════════
    # Gate: 顶部信号（看空加权拦截）
    # ═══════════════════════════════════════════
    t1 = check_weekly_top_divergence(weekly_df, weekly_hist)
    gate["周线顶背驰"] = t1

    t2 = bool(is_death_cross(weekly_dif, weekly_dea).tail(8).any())
    gate["周线死叉(近8周)"] = t2

    t3_death = False
    t3_dif_below = False
    if daily_df is not None and not daily_df.empty:
        daily_dif, daily_dea, _ = calc_macd(daily_df["close"])
        t3_death = bool(is_death_cross(daily_dif, daily_dea).tail(5).any())
        t3_dif_below = bool((daily_dif < 0).iloc[-1])
        gate["日线死叉"] = t3_death
        gate["日线DIF<0"] = t3_dif_below
    elif monthly_dif is not None and monthly_dea is not None:
        t3_death = bool(is_death_cross(monthly_dif, monthly_dea).tail(4).any())
        t3_dif_below = bool((monthly_dif < 0).iloc[-1])
        gate["月线死叉(回退)"] = t3_death
        gate["月线DIF<0(回退)"] = t3_dif_below

    # 看空加权得分
    bearish_weighted = (
        (1 if t1 else 0) * BEARISH_SIGNAL_WEIGHTS["weekly_top_divergence"] +
        (1 if t2 else 0) * BEARISH_SIGNAL_WEIGHTS["weekly_death_cross"] +
        (1 if t3_death else 0) * BEARISH_SIGNAL_WEIGHTS["daily_death_cross"] +
        (1 if t3_dif_below else 0) * BEARISH_SIGNAL_WEIGHTS["daily_dif_below_zero"]
    )
    gate["看空加权分"] = bearish_weighted
    bearish_score = sum([t1, t2, t3_death, t3_dif_below])
    gate["顶部得分"] = bearish_score

    # ═══════════════════════════════════════════
    # 判定: 看空加权≥阈值 → 拦截, bullish≥阈值 → 通过, 其余不通过
    # risk_warning 与 Gate 判定解耦: 存在看空信号即为风险提示
    # ═══════════════════════════════════════════
    risk_warning = bearish_score >= 1

    if bearish_weighted >= bearish_threshold:
        passed = False
    elif bullish_score >= gate_threshold:
        passed = True
    else:
        passed = False

    # ═══════════════════════════════════════════
    # 过门后评分 (0-100): 统一四维度
    # ═══════════════════════════════════════════
    score = 0.0
    if passed:
        w = score_weights or {"trend": 30, "alpha": 25, "vol": 25, "fund": 20}
        trend_base = _score_trend_5dim(weekly_dif, weekly_dea, weekly_hist, weekly_df["close"])
        # 中枢位置加成 (-3 ~ +3)
        try:
            from factors.chanlun.zhongshu import score_zhongshu_position_for_trend
            zs_bonus = score_zhongshu_position_for_trend(weekly_df)
        except Exception:
            zs_bonus = 0.0
        trend = max(0.0, min(30.0, trend_base + zs_bonus)) * w["trend"] / 30
        alpha = _score_alpha_cross_sectional(etf_name, cross_returns) * w["alpha"] / 25 if cross_returns else _score_alpha_vs_benchmark(weekly_df["close"], benchmark_close) * w["alpha"] / 25
        vol = score_volume_three_part(weekly_df["volume"], weekly_df["close"]) * w["vol"] / 100
        fund = fund_result["fund_score"]
        score = trend + alpha + vol + fund
        score = round(max(0.0, min(100.0, score)), 1)

    return SectorVerdict(
        etf_name=etf_name,
        etf_code=etf_code,
        passed_gate=passed,
        gate_details=gate,
        score=score,
        fund_confidence=fund_result["fund_confidence"],
        fund_level=fund_result["fund_level"],
        fund_note=fund_result["fund_note"],
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        risk_warning=risk_warning,
    )


def _assess_sector_fund(fund_data: dict | None,
                        weekly_df: pd.DataFrame | None = None
                        ) -> dict:
    """评估分类指数资金面，返回完整评估字典"""
    vol_health = 50.0
    trend_up = False
    if weekly_df is not None and not weekly_df.empty and len(weekly_df) >= 20:
        from factors.volume import score_volume_three_part, _detect_price_trend
        vol_health = score_volume_three_part(weekly_df["volume"], weekly_df["close"])
        trend_up = _detect_price_trend(weekly_df["close"]) == "up"

    if fund_data is None:
        return assess_fund_for_layer(for_layer=2, volume_health=vol_health, trend_up=trend_up)

    has_nb = fund_data.get("northbound_available", False)
    has_bo = fund_data.get("big_order_available", False)
    has_mg = fund_data.get("margin_available", False)
    has_to = fund_data.get("turnover_available", False)
    nb_flow = fund_data.get("northbound_net_flow", 0)
    amount_trend = fund_data.get("turnover_trend_up", False)

    return assess_fund_for_layer(
        has_northbound=has_nb, has_big_order=has_bo,
        has_margin=has_mg, has_turnover=has_to,
        northbound_inflow=nb_flow,
        amount_trend_up=amount_trend,
        margin_trend=fund_data.get("margin_trend", ""),
        big_order_direction=fund_data.get("big_order_direction", ""),
        for_layer=2, volume_health=vol_health, trend_up=trend_up,
    )


# ═══════════════════════════════════════════
# 趋势强度: 5维MACD评估 (0-30)
# ═══════════════════════════════════════════

def _score_trend_5dim(dif: pd.Series, dea: pd.Series,
                       hist: pd.Series, close: pd.Series) -> float:
    """5维MACD趋势强度 (0-30)"""
    if len(dif) < 5 or len(hist) < 5:
        return 15.0

    dif_val = float(dif.dropna().iloc[-1])
    dif_prev = float(dif.dropna().iloc[-5]) if len(dif.dropna()) >= 5 else dif_val

    # 1. 方向 (8分): DIF位置 + 方向
    dif_up = dif_val > dif_prev
    if dif_val > 0 and dif_up:
        direction = 8
    elif dif_val > 0 and not dif_up:
        direction = 5
    elif dif_val <= 0 and dif_up:
        direction = 4
    else:
        direction = 1

    # 2. 力度 (7分): MACD柱相对强度
    hist_vals = hist.dropna()
    hist_max = float(hist_vals.tail(20).abs().max()) if len(hist_vals) >= 20 else max(float(hist_vals.abs().max()), 1e-10)
    force = min(7.0, abs(float(hist_vals.iloc[-1])) / max(hist_max, 1e-10) * 7)

    # 3. 加速度 (5分): DIF斜率变化
    if len(dif.dropna()) >= 10:
        recent_slope = float(dif.dropna().iloc[-1]) - float(dif.dropna().iloc[-5])
        earlier_slope = float(dif.dropna().iloc[-5]) - float(dif.dropna().iloc[-10])
        if recent_slope > earlier_slope and recent_slope > 0:
            accel = 5
        elif recent_slope > earlier_slope:
            accel = 3
        elif recent_slope > 0:
            accel = 2
        else:
            accel = 1
    else:
        accel = 3

    # 4. 稳定性 (5分): 金叉持续周数
    golden_crosses = is_golden_cross(dif, dea)
    death_crosses = is_death_cross(dif, dea)
    weeks_since_golden = 0
    for i in range(len(golden_crosses) - 1, -1, -1):
        if death_crosses.iloc[i]:
            break
        if golden_crosses.iloc[i]:
            weeks_since_golden = len(golden_crosses) - i
            break
    else:
        # 没有金叉，检查是否一直处于金叉状态
        if dif_val > float(dea.dropna().iloc[-1]):
            weeks_since_golden = 1

    if weeks_since_golden > 13:
        stability = 5
    elif weeks_since_golden > 8:
        stability = 4
    elif weeks_since_golden > 5:
        stability = 3
    elif weeks_since_golden > 2:
        stability = 2
    else:
        stability = 1

    # 5. MA结构 (5分)
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1] if len(close) >= 60 else ma20
    if ma5 > ma20 > ma60:
        ma_struct = 5
    elif ma5 > ma20:
        ma_struct = 3
    elif ma20 > ma60:
        ma_struct = 2
    else:
        ma_struct = 1

    return float(direction + force + accel + stability + ma_struct)


# ═══════════════════════════════════════════
# Alpha强度: 横截面百分位排名 (0-25)
# ═══════════════════════════════════════════

def _score_alpha_cross_sectional(etf_name: str,
                                  cross_returns: dict[str, dict[str, float]] | None) -> float:
    """横截面Alpha: 在13只ETF中排名"""
    if not cross_returns or etf_name not in cross_returns:
        return 12.5  # 中性

    this = cross_returns[etf_name]
    periods = {"1w": 0.2, "4w": 0.4, "13w": 0.4}
    alpha_score = 0.0
    total_weight = 0.0

    for period, weight in periods.items():
        if period not in this:
            continue
        my_ret = this[period]
        all_rets = [v[period] for v in cross_returns.values() if period in v and not np.isnan(v[period])]
        if len(all_rets) < 2:
            alpha_score += 12.5 * weight
        else:
            better = sum(1 for r in all_rets if my_ret > r)
            pct = better / len(all_rets)
            alpha_score += pct * 25 * weight
        total_weight += weight

    if total_weight > 0:
        alpha_score = alpha_score / total_weight
    else:
        alpha_score = 12.5

    # 边际变化检测: 4周排名 vs 1周排名
    if "1w" in this and "4w" in this and cross_returns:
        all_4w = sorted([(k, v["4w"]) for k, v in cross_returns.items() if "4w" in v and not np.isnan(v["4w"])],
                        key=lambda x: x[1], reverse=True)
        all_1w = sorted([(k, v["1w"]) for k, v in cross_returns.items() if "1w" in v and not np.isnan(v["1w"])],
                        key=lambda x: x[1], reverse=True)
        rank_4w = next((i for i, (k, _) in enumerate(all_4w) if k == etf_name), len(all_4w))
        rank_1w = next((i for i, (k, _) in enumerate(all_1w) if k == etf_name), len(all_1w))
        if rank_4w > 0 and rank_1w / max(rank_4w, 1) > 1.05:
            alpha_score = min(25.0, alpha_score + 2.0)

    return round(min(25.0, max(0.0, alpha_score)), 1)


def _score_alpha_vs_benchmark(close: pd.Series, benchmark: pd.Series | None) -> float:
    """基准Alpha (无横截面数据时退火) — 返回0-25"""
    if benchmark is None or len(close) < 5 or len(benchmark) < 5:
        return 12.5
    stock_ret = close.pct_change(4).dropna()
    bench_ret = benchmark.pct_change(4).dropna()
    aligned = pd.concat([stock_ret, bench_ret], axis=1).dropna()
    if len(aligned) < 2:
        return 12.5
    excess = aligned.iloc[:, 0] - aligned.iloc[:, 1]
    recent_excess = float(excess.tail(4).mean())
    return min(25.0, max(0.0, (recent_excess * 10 + 0.5) * 25))


def _compute_cross_returns(etf_data: dict[str, pd.DataFrame]) -> dict[str, dict[str, float]]:
    """计算所有ETF的多周期收益率，供横截面Alpha排名使用"""
    cross = {}
    for name, df in etf_data.items():
        if df.empty or len(df) < 13:
            continue
        close = df["close"]
        rets = {}
        if len(close) >= 2:
            rets["1w"] = float(close.iloc[-1] / close.iloc[-2] - 1)
        if len(close) >= 5:
            rets["4w"] = float(close.iloc[-1] / close.iloc[-5] - 1)
        if len(close) >= 14:
            rets["13w"] = float(close.iloc[-1] / close.iloc[-14] - 1)
        cross[name] = rets
    return cross


def assess_sectors(
    etf_data: dict[str, pd.DataFrame],
    monthly_data: dict[str, pd.DataFrame] | None = None,
    daily_data: dict[str, pd.DataFrame] | None = None,
    benchmark_close: pd.Series | None = None,
    fund_data: dict[str, dict] | None = None,
    top_k_ratio: float = 0.30,
    min_top_k: int = 3,
    score_weights: dict[str, float] | None = None,
    l1_market_state: str = "volatile",
) -> Layer2Result:
    """
    评估所有ETF分类指数，返回强势指数列表。

    两阶段选择 + 横截面Alpha + L1联动阈值。
    l1_market_state: "bull" | "volatile" | "weak" | "bear"
        - bull: Gate需要3-of-5
        - volatile/weak: Gate需要2-of-5
        - bear: 跳过L2，直接返回空
    """
    if l1_market_state == "bear":
        return Layer2Result(
            candidate_sectors=[], strong_sectors=[], passed=False,
            details=["L1熊市 → 跳过L2"], risk_warning=False,
        )

    # 预计算横截面收益率
    cross_returns = _compute_cross_returns(etf_data)

    # 动态阈值
    gate_threshold = _GATE_THRESHOLD_MAP.get(l1_market_state, 2)
    bearish_threshold = _BEARISH_THRESHOLD_MAP.get(l1_market_state, 4)

    all_verdicts = []
    candidates = []
    details = []
    pattern_results = {}  # etf_name → PatternClassification

    for etf_name, weekly_df in etf_data.items():
        if weekly_df.empty or len(weekly_df) < 26:
            details.append(f"{etf_name}: 数据不足")
            continue

        code = ETF_INDEX_CODES.get(etf_name, "unknown")
        monthly = monthly_data.get(etf_name) if monthly_data else None
        fund = fund_data.get(etf_name) if fund_data else None
        daily = daily_data.get(etf_name) if daily_data else None

        verdict = assess_single_sector(
            etf_name=etf_name,
            etf_code=code,
            weekly_df=weekly_df,
            monthly_df=monthly,
            daily_df=daily,
            benchmark_close=benchmark_close,
            fund_data=fund,
            score_weights=score_weights,
            cross_returns=cross_returns,
            gate_threshold=gate_threshold,
            bearish_threshold=bearish_threshold,
        )

        all_verdicts.append(verdict)

        if verdict.passed_gate:
            candidates.append(verdict)
            details.append(f"{etf_name}: 通过Gate (评分={verdict.score:.1f}, 资金={verdict.fund_level})")
        else:
            failed = [k for k, v in verdict.gate_details.items() if isinstance(v, bool) and not v]
            details.append(f"{etf_name}: Gate不通过 — {failed}")

    # ── 形态结构分类：评分修正 + 强看空过滤 ──
    if candidates:
        from gate.layer2_patterns import classify_sector_pattern, get_pattern_bonus

        # --- 步骤1: 形态分类与评分修正 ---
        for v in candidates:
            wdf = etf_data.get(v.etf_name)
            if wdf is not None and not wdf.empty:
                pat = classify_sector_pattern(v.etf_name, wdf)
                pattern_results[v.etf_name] = pat
                bonus = get_pattern_bonus(pat, l1_market_state)
                v.pattern_bonus = bonus
                v.score = round(max(0.0, min(100.0, v.score + bonus)), 1)
                if bonus != 0:
                    details.append(f"{v.etf_name}: 形态修正 {bonus:+.1f} → 最终评分={v.score:.1f}")

        # --- 步骤2: 强看空形态过滤（PRIMARY filter，非仅评分修正）---
        filter_threshold = -8 if l1_market_state == "bull" else -5
        for v in candidates:
            if v.pattern_bonus <= filter_threshold:
                v.pattern_filtered = True
                details.append(
                    f"{v.etf_name}: 形态过滤排除 (bonus={v.pattern_bonus:.1f} ≤ {filter_threshold})"
                )

        # 从候选列表中移除被形态过滤的板块
        before = len(candidates)
        candidates = [v for v in candidates if not v.pattern_filtered]
        filtered_count = before - len(candidates)
        if filtered_count:
            details.append(f"形态过滤: 排除 {filtered_count} 个板块")

        details.append(f"形态修正后: {len(candidates)} 候选 (状态={l1_market_state})")

    candidates = sorted(candidates, key=lambda v: v.score, reverse=True)
    k = default_top_k(len(candidates), top_k_ratio, min_top_k)
    strong = candidates[:k]

    details.append(f"最终: {len(candidates)} 候选 | {len(strong)} 强势 (TopK={k}, 门槛={gate_threshold}of5)")

    has_risk = any(v.risk_warning for v in all_verdicts)
    return Layer2Result(
        candidate_sectors=candidates,
        strong_sectors=strong,
        passed=len(strong) > 0,
        details=details,
        risk_warning=has_risk,
    )


# ETF代码速查
ETF_INDEX_CODES = {
    "证券": "399975", "银行": "399986", "军工": "399967",
    "芯片": "990001", "半导体": "sz399678", "新能源车": "399976", "光伏": "931151",
    "消费": "000932", "医药": "000933", "酒": "399997",
    "科技": "931079", "有色": "000819", "煤炭": "399998",
    "汽车": "931008",
}


# ── 第二层日频预警（V2.5/V2.9）──


def check_daily_alert(
    etf_daily: dict[str, pd.DataFrame],        # {ETF名称: df_daily}
    held_etf_map: dict[str, list[str]],         # {symbol: [etf_names]}
    alert_active_etfs: list[str] = None,         # 已有预警的ETF列表（叠加用）
) -> dict:
    """
    第二层日频预警：检查已持仓股票所属ETF指数的日线状态。

    触发条件:
    - ETF指数日线跌破20日均线 + 日线MACD死叉
    - ETF指数日线级别顶背驰

    预警分级:
    - 黄色: 1-2个ETF触发 → 冻结该指数买入，该指数下仓位减半
    - 橙色: 3-5个ETF触发 → 冻结所有触发指数买入，总仓位降一档
    - 红色: 超半数持仓指数触发 → 冻结全部买入，仓位降到最低档(≤30%)

    返回: {triggered: bool, level: str, triggered_etfs: [str], actions: [str]}
    """
    from factors.macd import check_daily_macd_above_ma20
    from factors.chanlun.divergence import check_daily_top_divergence

    # 收集所有持仓ETF（去重）
    held_etfs: set[str] = set()
    for etfs in held_etf_map.values():
        held_etfs.update(etfs)

    if not held_etfs:
        return {"triggered": False, "level": "无预警", "triggered_etfs": [], "actions": []}

    triggered_etfs = list(alert_active_etfs) if alert_active_etfs else []

    for etf_name in held_etfs:
        if etf_name in triggered_etfs:
            continue
        daily_df = etf_daily.get(etf_name)
        if daily_df is None or daily_df.empty or len(daily_df) < 26:
            continue

        result = check_daily_macd_above_ma20(daily_df["close"])
        macd_dead = not result["macd_ok"]
        below_ma20 = not result["ma_ok"]
        has_top_div = check_daily_top_divergence(daily_df, result["hist"])

        if (macd_dead and below_ma20) or has_top_div:
            triggered_etfs.append(etf_name)

    if not triggered_etfs:
        return {"triggered": False, "level": "无预警", "triggered_etfs": [], "actions": []}

    # 分级
    total_held = len(held_etfs)
    triggered_count = len(triggered_etfs)
    actions = []

    if triggered_count >= total_held * 0.67 and triggered_count >= 2:
        level = "红色预警"
        actions = ["冻结全部买入", "总仓位降到最低档(≤30%)"]
    elif triggered_count >= 3:
        level = "橙色预警"
        actions = ["冻结所有触发指数买入", "总仓位降一档"]
    else:
        level = "黄色预警"
        actions = [f"冻结 {', '.join(triggered_etfs)} 买入", "该指数下仓位减半"]

    logger.warning(f"L2日频预警 {level}: {triggered_etfs}")
    return {
        "triggered": True,
        "level": level,
        "triggered_etfs": triggered_etfs,
        "actions": actions,
    }
