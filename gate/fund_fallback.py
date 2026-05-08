"""资金面降级策略 + 技术推断补充"""

from core.logger import get_logger

logger = get_logger("gate.fund_fallback")


def determine_fund_level(has_northbound: bool, has_big_order: bool = False,
                         has_margin: bool = False, has_turnover: bool = False,
                         northbound_inflow: float = 0, big_order_direction: str = "",
                         margin_trend: str = "", amount_trend_up: bool = False,
                         for_layer: int = 1) -> tuple[str, float, bool, str]:
    """
    确定资金面降级等级和可信度。

    层级:
    - L1: 北向+融资+大单 三者齐全 (个股/ETF) / 北向+成交额 (大盘)
    - L2: 北向+大单 (无融资) / 仅北向
    - L3: 仅大单 / 仅融资 / 仅成交额 (量价替代)
    - L4: 纯技术面 (资金面缺失)

    for_layer: 1=大盘, 2=分类指数, 3=个股

    返回: (level, confidence, pass, note)
    """
    c = get_confidence_discount

    if for_layer == 1:
        # --- 大盘级别: 北向 + 成交额 ---
        if has_northbound and has_turnover:
            if northbound_inflow > 0 and amount_trend_up:
                return "L1", c("L1"), True, "资金完整确认"
            elif northbound_inflow < 0:
                return "L1", c("L1"), False, "北向流出，技术资金不一致"
            else:
                return "L1", c("L1"), True, "资金确认"

        if has_northbound:
            if northbound_inflow > 0:
                return "L2", c("L2"), True, "仅北向确认"
            else:
                return "L2", c("L2"), False, "北向流出"

        if has_turnover:
            if amount_trend_up:
                return "L3", c("L3"), True, "量价替代"
            else:
                return "L3", c("L3"), False, "成交量不健康"

        return "L4", c("L4"), True, "资金面缺失"

    else:
        # --- 分类指数/个股级别: 北向 + 大单 + 融资 ---
        if has_northbound and has_big_order and has_margin:
            if northbound_inflow > 0:
                return "L1", c("L1"), True, "资金完整确认"
            else:
                return "L1", c("L1"), False, "资金方向与价格反向"

        if has_northbound and has_big_order:
            if northbound_inflow > 0:
                return "L2", c("L2"), True, "北向+大单确认"
            else:
                return "L2", c("L2"), False, "北向流出"

        if has_northbound and has_margin:
            if northbound_inflow > 0 and margin_trend == "up":
                return "L1", 1.0, True, "北向+融资一致"
            elif northbound_inflow > 0 or margin_trend == "up":
                return "L2", 0.85, True, "北向/融资部分一致"

        if has_big_order and has_margin:
            if big_order_direction == "流入" and margin_trend == "up":
                return "L2", 0.85, True, "大单+融资一致"
            elif big_order_direction == "流入" or margin_trend == "up":
                return "L3", 0.75, True, "大单/融资部分一致"

        if has_northbound:
            if northbound_inflow > 0:
                return "L2", 0.80, True, "仅北向确认"
            else:
                return "L2", 0.80, False, "北向流出"

        if has_big_order:
            if big_order_direction == "流入":
                return "L3", 0.75, True, "仅大单确认"
            else:
                return "L3", 0.75, False, "大单流出"

        if has_margin:
            if margin_trend == "up":
                return "L3", 0.75, True, "仅融资确认"
            else:
                return "L3", 0.75, False, "融资趋势向下"

        if has_turnover:
            if amount_trend_up:
                return "L3", c("L3"), True, "量价替代"
            else:
                return "L3", c("L3"), False, "成交量不健康"

        return "L4", c("L4"), True, "资金面缺失"


def score_fund_with_technical(
    fund_level: str, fund_confidence: float, fund_pass: bool,
    volume_health: float = 50.0, trend_up: bool = False,
) -> tuple[float, str]:
    """
    双路径资金力度评分 (0-20)。

    直接信号分 = 资金面置信度 × 20
    技术推断分 = 根据量价健康度和趋势推断 (4-12)
    数据权重 = 资金面置信度 (L1=1.0 完全信任直接信号, L4=0.80 部分依赖技术推断)

    返回: (fund_score, note)
    """
    direct_score = fund_confidence * 20

    # 技术推断: 量价健康 + 趋势向上 → 推断资金流入
    if volume_health >= 60 and trend_up:
        tech_score = 14.0
        tech_note = "技术推断→资金流入概率高"
    elif volume_health >= 60 and not trend_up:
        tech_score = 10.0
        tech_note = "量价健康但趋势不明"
    elif volume_health >= 40 and trend_up:
        tech_score = 8.0
        tech_note = "趋势向上但量能一般"
    elif volume_health >= 40:
        tech_score = 6.0
        tech_note = "量能与趋势均一般"
    else:
        tech_score = 4.0
        tech_note = "技术面偏弱"

    # 直接信号的权重 = 置信度，技术推断的权重 = 1 - 置信度
    data_weight = fund_confidence
    fund_score = direct_score * data_weight + tech_score * (1 - data_weight)

    note = f"直接{fund_level}({direct_score:.0f}×{data_weight:.2f}) + {tech_note}({tech_score:.0f}×{1-data_weight:.2f})"
    return round(fund_score, 1), note


def assess_fund_for_layer(
    has_northbound: bool = False, has_big_order: bool = False,
    has_margin: bool = False, has_turnover: bool = False,
    northbound_inflow: float = 0, big_order_direction: str = "",
    margin_trend: str = "", amount_trend_up: bool = False,
    for_layer: int = 1, volume_health: float = 50.0, trend_up: bool = False,
) -> dict:
    """
    一站式资金面评估，供 Gate 层直接调用。

    返回 dict: {
        "fund_level": str,
        "fund_confidence": float,
        "fund_pass": bool,
        "fund_score": float (0-20),
        "fund_note": str,
    }
    """
    level, confidence, passed, raw_note = determine_fund_level(
        has_northbound=has_northbound, has_big_order=has_big_order,
        has_margin=has_margin, has_turnover=has_turnover,
        northbound_inflow=northbound_inflow, big_order_direction=big_order_direction,
        margin_trend=margin_trend, amount_trend_up=amount_trend_up,
        for_layer=for_layer,
    )

    # L4 资金面缺失时，技术推断决定通过/不通过（量价健康>35 AND 趋势向上才通过）
    # vol_health 历史分布(周线): p25≈46, p50≈51, p90≈67，阈值35覆盖约90%的场景
    if level == "L4":
        passed = volume_health >= 35 and trend_up

    fund_score, score_note = score_fund_with_technical(
        fund_level=level, fund_confidence=confidence, fund_pass=passed,
        volume_health=volume_health, trend_up=trend_up,
    )

    return {
        "fund_level": level,
        "fund_confidence": confidence,
        "fund_pass": passed,
        "fund_score": fund_score,
        "fund_note": f"{raw_note}; {score_note}",
    }


def downgrade_fund_data(missing_sources: list[str], for_layer: int = 1) -> dict:
    """
    根据缺失的数据源列表生成降级后的资金数据字典。

    missing_sources: 不可用的数据源列表，如 ['northbound', 'margin']
    返回: 可用于资金面评估的数据字典
    """
    available = {
        "northbound": "northbound" not in missing_sources,
        "big_order": "big_order" not in missing_sources,
        "margin": "margin" not in missing_sources,
        "turnover": "turnover" not in missing_sources,
    }
    logger.info(f"资金源可用性 (L{for_layer}): {available}")
    return available


# 运行时参数覆盖（供 auto_tune 使用）
_tune_confidence_map: dict[str, float] | None = None


def get_confidence_discount(level: str) -> float:
    """获取可信度折扣系数"""
    default = {"L1": 1.0, "L2": 0.85, "L3": 0.85, "L4": 0.80}
    if _tune_confidence_map is not None:
        return _tune_confidence_map.get(level, 0.80)
    return default.get(level, 0.80)
