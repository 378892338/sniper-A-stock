"""第一层门卫：大盘环境评估（周线）— 统一评分框架"""

from dataclasses import dataclass, field

import pandas as pd

from factors.macd import (
    calc_macd, is_golden_cross, is_dif_above_dea,
    is_hist_positive, is_dif_above_zero, is_dif_turning_up,
    is_death_cross,
)
from factors.chanlun.divergence import (
    check_weekly_top_divergence, check_weekly_bottom_divergence,
    check_monthly_bottom_divergence,
)
from factors.volume import score_volume_three_part, is_amount_trending_up
from gate.fund_fallback import assess_fund_for_layer
from core.logger import get_logger

logger = get_logger("gate.layer1")

# 看空信号加权阈值（L1用）
L1_BEARISH_WEIGHTS = {"weekly_top_divergence": 3, "weekly_death_cross": 2, "monthly_death_or_below": 2}
L1_BEARISH_INTERCEPT = 4


@dataclass
class MarketVerdict:
    """单个市场评估结果"""
    name: str  # shanghai / shenzhen / chinext
    is_strong: bool
    tech_pass: bool
    tech_details: dict = field(default_factory=dict)
    fund_level: str = "L1"
    fund_confidence: float = 1.0
    fund_pass: bool = True
    fund_note: str = ""
    fund_score: float = 0.0   # 资金力度 (0-30)
    score: float = 0.0        # 综合评分 (0-100): Trend40+Volume30+Fund30
    bullish_score: int = 0    # 底部共振条件命中数 (0-3)
    bearish_score: int = 0    # 顶部信号条件命中数 (0-3)
    bearish_weighted: int = 0 # 看空加权分
    risk_warning: bool = False


@dataclass
class Layer1Result:
    """第一层综合结果"""
    verdicts: dict[str, MarketVerdict]  # {market_name: verdict}
    strong_count: int
    market_state: str  # "牛市"/"震荡"/"偏弱"/"熊市"
    base_position_pct: float
    actual_position_pct: float
    passed: bool
    details: list[str] = field(default_factory=list)
    risk_warning: bool = False
    bottom_bullish_markets: int = 0
    avg_score: float = 0.0  # 三市平均分
    yin_die_triggered: bool = False  # §9: 任一市场检测到阴跌
    yin_die_markets: list[str] = field(default_factory=list)


def assess_single_market(
    name: str,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame | None = None,
    fund_data: dict | None = None,
) -> MarketVerdict:
    """
    2-of-3 计分制评估 + 统一四维度评分 (Trend40+Volume30+Fund30)。

    看空加权拦截: 周线顶背驰×3 + 周线死叉×2 + 月线死叉/DIF<0×2 ≥ 5 → 拦截
    """
    tech_details = {}
    weekly_dif, weekly_dea, weekly_hist = calc_macd(weekly_df["close"])
    monthly_dif, monthly_dea, monthly_hist = (
        calc_macd(monthly_df["close"]) if monthly_df is not None
        else (None, None, None)
    )

    # ── 底部共振条件 ──
    b1_weekly_golden = bool(is_golden_cross(weekly_dif, weekly_dea).tail(8).any())
    tech_details["周线金叉(近8周)"] = b1_weekly_golden

    b3_bottom_div = check_weekly_bottom_divergence(weekly_df, weekly_hist)
    tech_details["周线底背驰"] = b3_bottom_div

    b2_monthly = False
    if monthly_dif is not None and monthly_dea is not None:
        monthly_golden = bool(is_golden_cross(monthly_dif, monthly_dea).tail(4).any())
        monthly_dif_above_zero = bool(is_dif_above_zero(monthly_dif).iloc[-1])
        b2_monthly = monthly_golden or monthly_dif_above_zero
        tech_details["月线金叉"] = monthly_golden
        tech_details["月线DIF>0"] = monthly_dif_above_zero
    else:
        tech_details["月线"] = "数据缺失"

    bullish_score = sum([b1_weekly_golden, b2_monthly, b3_bottom_div])
    tech_details["底部得分"] = bullish_score

    # ── 顶部信号 ──
    t1_top_div = check_weekly_top_divergence(weekly_df, weekly_hist)
    tech_details["周线顶背驰"] = t1_top_div

    t2_weekly_death = bool(is_death_cross(weekly_dif, weekly_dea).tail(8).any())
    tech_details["周线死叉(近8周)"] = t2_weekly_death

    t3_monthly = False
    if monthly_dif is not None and monthly_dea is not None:
        monthly_death = bool(is_death_cross(monthly_dif, monthly_dea).tail(4).any())
        monthly_dif_below_zero = bool((monthly_dif < 0).iloc[-1])
        t3_monthly = monthly_death or monthly_dif_below_zero
        tech_details["月线死叉"] = monthly_death
        tech_details["月线DIF<0"] = monthly_dif_below_zero

    bearish_score = sum([t1_top_div, t2_weekly_death, t3_monthly])
    bearish_weighted = (
        (1 if t1_top_div else 0) * L1_BEARISH_WEIGHTS["weekly_top_divergence"] +
        (1 if t2_weekly_death else 0) * L1_BEARISH_WEIGHTS["weekly_death_cross"] +
        (1 if t3_monthly else 0) * L1_BEARISH_WEIGHTS["monthly_death_or_below"]
    )
    tech_details["顶部得分"] = bearish_score
    tech_details["看空加权分"] = bearish_weighted

    # ── 阴跌检测（针对顶背驰不触发的慢熊市）──
    yin_die = False
    close_vals = weekly_df["close"]
    n_wk = len(close_vals)
    if n_wk >= 13:
        ret_60d = float(close_vals.iloc[-1] / close_vals.iloc[-13] - 1)      # 12周≈60日
        ret_20d = float(close_vals.iloc[-1] / close_vals.iloc[-5] - 1)       # 4周≈20日
        ma_12w = float(close_vals.rolling(12).mean().iloc[-1])
        below_ma60_equiv = float(close_vals.iloc[-1]) < ma_12w
        yin_die = (ret_60d < -0.10) and below_ma60_equiv and (ret_20d < -0.05)
    tech_details["阴跌"] = yin_die

    # ── 判定: 看空加权≥4 或 阴跌 → 拦截 ──
    if bearish_weighted >= L1_BEARISH_INTERCEPT or yin_die:
        tech_pass = False
        risk_warning = False
    elif bullish_score >= 2:
        tech_pass = True
        risk_warning = False
    elif bearish_score == 1:
        tech_pass = True
        risk_warning = True
    else:
        tech_pass = True
        risk_warning = False

    # 资金面: 使用统一评估函数
    fund_result = _assess_l1_fund(weekly_df, fund_data)
    fund_pass = fund_result["fund_pass"]
    is_strong = tech_pass and fund_pass

    # ── 过门后评分 (0-100): Trend40 + Volume30 + Fund30 ──
    score = 0.0
    if is_strong:
        from gate.layer2_sector import _score_trend_5dim
        trend = _score_trend_5dim(weekly_dif, weekly_dea, weekly_hist, weekly_df["close"])
        # 中枢位置加成 (-3 ~ +3)
        try:
            from factors.chanlun.zhongshu import score_zhongshu_position_for_trend
            zs_bonus = score_zhongshu_position_for_trend(weekly_df)
        except Exception:
            zs_bonus = 0.0
        trend_scaled = (trend / 30) * 40 + zs_bonus
        trend_scaled = max(0.0, min(40.0, trend_scaled))
        vol = score_volume_three_part(weekly_df["volume"], weekly_df["close"])
        vol_scaled = (vol / 100) * 30
        fund_scaled = fund_result["fund_score"] * (30 / 20)
        score = round(max(0.0, min(100.0, trend_scaled + vol_scaled + fund_scaled)), 1)

    return MarketVerdict(
        name=name,
        is_strong=is_strong,
        tech_pass=tech_pass,
        tech_details=tech_details,
        fund_level=fund_result["fund_level"],
        fund_confidence=fund_result["fund_confidence"],
        fund_pass=fund_pass,
        fund_note=fund_result["fund_note"],
        fund_score=fund_result["fund_score"],
        score=score,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        bearish_weighted=bearish_weighted,
        risk_warning=risk_warning,
    )


def _assess_l1_fund(weekly_df: pd.DataFrame, fund_data: dict | None) -> dict:
    """L1资金面评估，使用统一 fund_fallback 模块"""
    vol_health = 50.0
    trend_up = False
    if weekly_df is not None and not weekly_df.empty and len(weekly_df) >= 20:
        vol_health = score_volume_three_part(weekly_df["volume"], weekly_df["close"])
        from factors.volume import _detect_price_trend
        trend_up = _detect_price_trend(weekly_df["close"]) == "up"

    if fund_data is None:
        return assess_fund_for_layer(for_layer=1, volume_health=vol_health, trend_up=trend_up)

    has_nb = fund_data.get("northbound_available", False)
    has_to = fund_data.get("turnover_available", False)
    nb_flow = fund_data.get("northbound_net_flow", 0)
    amount_trend = fund_data.get("turnover_trend_up", False)

    return assess_fund_for_layer(
        has_northbound=has_nb, has_turnover=has_to,
        northbound_inflow=nb_flow, amount_trend_up=amount_trend,
        for_layer=1, volume_health=vol_health, trend_up=trend_up,
    )


def assess_market(
    market_data: dict[str, pd.DataFrame],
    fund_data: dict[str, dict] | None = None,
    monthly_data: dict[str, pd.DataFrame] | None = None,
    position_map: dict[str, float] | None = None,
    pass_min_strong: int = 2,
) -> Layer1Result:
    """
    评估大盘环境，返回牛市/震荡/偏弱/熊市判定。

    market_data: {shanghai: df_weekly, shenzhen: df_weekly, chinext: df_weekly}
    fund_data: {shanghai: {northbound_available, northbound_net_flow, ...}, ...}
    monthly_data: {shanghai: df_monthly, ...}
    position_map: 自定义仓位映射 {"牛市": 1.0, "震荡": 0.50, "偏弱": 0.30, "熊市": 0.0}
    pass_min_strong: 最少几个市场强势才算通过
    """
    if position_map is None:
        position_map = {"牛市": 1.0, "震荡": 0.70, "偏弱": 0.30, "熊市": 0.0}

    names_map = {
        "shanghai": "上证指数",
        "shenzhen": "深证成指",
        "chinext": "创业板指",
    }

    verdicts = {}
    for market_name, weekly_df in market_data.items():
        if weekly_df.empty:
            continue
        fund = fund_data.get(market_name) if fund_data else None
        monthly = monthly_data.get(market_name) if monthly_data else None
        verdicts[market_name] = assess_single_market(market_name, weekly_df, monthly_df=monthly, fund_data=fund)

    strong_count = sum(1 for v in verdicts.values() if v.is_strong)
    bottom_bullish = sum(1 for v in verdicts.values() if v.bullish_score >= 2)
    has_risk_warning = any(v.risk_warning for v in verdicts.values())

    # §9: 阴跌检测 — 任一市场检测到阴跌即触发
    yin_die_markets = [names_map.get(k, k) for k, v in verdicts.items() if v.tech_details.get("阴跌", False)]
    yin_die_triggered = len(yin_die_markets) > 0

    # ── 创业板一票否决（P1）──
    cy = verdicts.get("chinext")
    chinext_weak = cy is not None and not cy.is_strong
    chinext_bearish = cy is not None and (cy.bearish_weighted >= L1_BEARISH_INTERCEPT or cy.tech_details.get("阴跌", False))

    # 判定市场状态（考虑底部共振）
    # R2: 3市全强但底部共振<2 → 震荡（底部共振不足降级）
    if strong_count == 3 and bottom_bullish >= 2:
        state = "牛市"
    elif strong_count == 3:
        state = "震荡"
    elif strong_count == 2:
        state = "偏弱"
    elif strong_count == 1:
        state = "偏弱"   # 动态仓位：1市强势=3成仓而非空仓
    else:
        state = "熊市"

    # 创业板熊市 → 整体状态上限为"偏弱"
    if chinext_bearish and state not in ("熊市", "偏弱"):
        state = "偏弱"
    elif chinext_weak and state not in ("熊市",):
        # 创业板弱势 + 上证/深证强势 → 降为"偏弱"
        if state in ("牛市", "震荡"):
            state = "偏弱"

    base_position = position_map.get(state, 0.0)

    # 置信度修正
    min_confidence = min((v.fund_confidence for v in verdicts.values()), default=1.0)

    # 风险折扣：任一市场出现1个顶部信号 → 仓位 × 0.7
    risk_discount = 0.7 if has_risk_warning else 1.0
    actual_position = base_position * min_confidence * risk_discount

    # 动态仓位：不再二值通过/不通过，position_pct 连续表达仓位
    passed = actual_position > 0

    avg_score = sum(v.score for v in verdicts.values()) / max(len(verdicts), 1)

    details = []
    for name, v in verdicts.items():
        cn_name = names_map.get(name, name)
        status = "强" if v.is_strong else "弱"
        b_score = f"底{v.bullish_score}/3" if v.bullish_score else ""
        t_score = f"顶{v.bearish_score}/3" if v.bearish_score else ""
        warn = " ⚠风险提示" if v.risk_warning else ""
        details.append(
            f"{cn_name}: {status} | 评分{v.score:.1f} | {b_score}{' ' if b_score and t_score else ''}{t_score}"
            f" | 资金={v.fund_level}(×{v.fund_confidence}){warn}"
        )
    risk_note = f" | 风险折扣×0.7" if has_risk_warning else ""
    details.append(
        f"判定: {state} | 底部共振{bottom_bullish}/3市 | 均分{avg_score:.1f} | "
        f"仓位{base_position*100:.0f}%→{actual_position*100:.0f}%{risk_note}"
    )

    return Layer1Result(
        verdicts=verdicts,
        strong_count=strong_count,
        market_state=state,
        base_position_pct=base_position,
        actual_position_pct=actual_position,
        passed=passed,
        details=details,
        risk_warning=has_risk_warning,
        bottom_bullish_markets=bottom_bullish,
        avg_score=avg_score,
        yin_die_triggered=yin_die_triggered,
        yin_die_markets=yin_die_markets,
    )


def daily_alert_check(
    daily_data: dict[str, pd.DataFrame],
    last_weekly_result: Layer1Result | None = None,
) -> dict:
    """
    第一层日频预警。

    触发条件:
    1. 任一指数跌破20日均线 + 日线MACD死叉
    2. 两个市场日线MACD死叉
    3. 上次周线评估为1强2弱或更弱 + 三个市场日线MACD全部死叉

    返回: {triggered: bool, level: "黄色"/"橙色"/"红色", details: [...]}
    """
    triggered_markets = []
    death_cross_markets = []

    for name, daily_df in daily_data.items():
        if daily_df.empty or len(daily_df) < 26:
            continue

        close = daily_df["close"]
        ma20 = close.rolling(20).mean()
        dif, dea, _ = calc_macd(close)
        has_death_cross = bool(is_death_cross(dif, dea).tail(3).any())

        if has_death_cross:
            death_cross_markets.append(name)

        below_ma20 = bool(close.iloc[-1] < ma20.iloc[-1]) if not pd.isna(ma20.iloc[-1]) else False

        # 条件1: 跌破20日均线 + 日线MACD死叉
        if below_ma20 and has_death_cross:
            triggered_markets.append(name)

    # 条件2: 两个市场日线MACD死叉
    if len(death_cross_markets) >= 2:
        triggered_markets = list(set(triggered_markets + death_cross_markets))

    # 条件3: 上次为1强2弱 + 三市全死叉
    if last_weekly_result and last_weekly_result.strong_count <= 1:
        if len(death_cross_markets) >= 3:
            triggered_markets = list(death_cross_markets)

    if not triggered_markets:
        return {"triggered": False, "level": None, "details": ["无预警"]}

    n = len(set(triggered_markets))
    if n >= 3:
        level = "红色"
    elif n >= 2:
        level = "橙色"
    else:
        level = "黄色"

    return {
        "triggered": True,
        "level": level,
        "details": [f"触发市场: {triggered_markets}", f"预警级别: {level}"],
    }
