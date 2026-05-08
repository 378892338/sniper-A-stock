"""L2 形态结构分类过滤 — 中枢/背驰/笔/MA排列作为一级过滤器"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from factors.macd import calc_macd, is_golden_cross
from factors.chanlun.divergence import (
    check_weekly_top_divergence, check_weekly_bottom_divergence,
)
from core.logger import get_logger

logger = get_logger("gate.layer2_patterns")


@dataclass
class PatternClassification:
    """单个ETF/板块的形态结构分类"""
    etf_name: str
    zhongshu_position: str = "none"       # "above"/"inside"/"below"/"none"
    divergence_type: str = "none"         # "top"/"bottom"/"none"
    stroke_direction: str = "neutral"     # "up"/"down"/"neutral"
    ma_arrangement: str = "mixed"         # "bull"/"bear"/"mixed"
    pattern_quality: float = 0.0          # 0-10 形态清晰度
    filter_pass: bool = True              # 是否通过形态过滤
    filter_reason: str = ""


def _detect_stroke_direction(weekly_df: pd.DataFrame) -> tuple[str, float]:
    """检测最近一笔的方向。返回 (direction, quality)"""
    from factors.chanlun.fractal import identify_fractals, filter_valid_fractals
    from factors.chanlun.stroke import divide_strokes

    try:
        df_frac = filter_valid_fractals(identify_fractals(weekly_df))
        df_stroke = divide_strokes(df_frac)
        if "stroke_type" not in df_stroke.columns:
            return "neutral", 3.0

        strokes = df_stroke["stroke_type"].dropna()
        if strokes.empty:
            return "neutral", 3.0

        last = strokes.iloc[-1]
        if last == 1:
            return "up", min(8.0, len(strokes) * 0.5)
        elif last == -1:
            return "down", min(8.0, len(strokes) * 0.5)
        return "neutral", 3.0
    except Exception:
        return "neutral", 3.0


def _detect_zhongshu_position(weekly_df: pd.DataFrame) -> tuple[str, float]:
    """检测价格相对中枢位置。返回 (position, quality)"""
    from factors.chanlun.zhongshu import identify_zhongshu

    try:
        zhongshus = identify_zhongshu(weekly_df)
        if not zhongshus or "close" not in weekly_df.columns:
            return "none", 0.0

        zs = zhongshus[-1]
        if zs.ZG <= zs.ZD:
            return "none", 0.0

        price = float(weekly_df["close"].iloc[-1])
        quality = min(10.0, len(zhongshus) * 2.5)

        if price > zs.ZG:
            return "above", quality
        elif price < zs.ZD:
            return "below", quality
        else:
            return "inside", quality
    except Exception:
        return "none", 0.0


def _detect_ma_arrangement(weekly_df: pd.DataFrame) -> str:
    """检测MA排列方式"""
    close = weekly_df["close"]
    n = len(close)
    if n < 60:
        return "mixed"

    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1])

    if ma5 > ma20 > ma60:
        return "bull"
    elif ma5 < ma20 < ma60:
        return "bear"
    return "mixed"


def classify_sector_pattern(etf_name: str, weekly_df: pd.DataFrame) -> PatternClassification:
    """
    对单个ETF/板块做形态结构分类。

    返回 PatternClassification，包含中枢位置、背驰类型、笔方向、MA排列。
    """
    _, _, weekly_hist = calc_macd(weekly_df["close"])

    zs_pos, zs_quality = _detect_zhongshu_position(weekly_df)
    stroke_dir, stroke_quality = _detect_stroke_direction(weekly_df)
    ma = _detect_ma_arrangement(weekly_df)

    # 背驰检测
    has_top_div = check_weekly_top_divergence(weekly_df, weekly_hist)
    has_bottom_div = check_weekly_bottom_divergence(weekly_df, weekly_hist)

    if has_top_div:
        div_type = "top"
    elif has_bottom_div:
        div_type = "bottom"
    else:
        div_type = "none"

    # 形态清晰度: 中枢质量 + 笔质量
    quality = round((zs_quality + stroke_quality) / 2, 1)

    return PatternClassification(
        etf_name=etf_name,
        zhongshu_position=zs_pos,
        divergence_type=div_type,
        stroke_direction=stroke_dir,
        ma_arrangement=ma,
        pattern_quality=quality,
    )


def apply_pattern_filter(
    patterns: dict[str, PatternClassification],
    market_state: str = "volatile",
) -> dict[str, PatternClassification]:
    """
    根据市场状态对已分类的ETF/板块做形态过滤。

    过滤规则:
    - 牛市: 排除顶背驰+中枢下方的板块（最宽松）
    - 震荡: 排除顶背驰板块，中枢下方降优先级
    - 偏弱: 只保留中枢上方+MA多头的板块（最严格）
    - 熊市: 全部不通过（L2跳过）

    返回: 过滤后的 patterns 字典（通过者 filter_pass=True）
    """
    if market_state == "bear":
        for p in patterns.values():
            p.filter_pass = False
            p.filter_reason = "熊市跳过L2"
        return patterns

    for name, p in patterns.items():
        p.filter_pass = True
        p.filter_reason = ""

        if market_state == "bull":
            # 排除: 顶背驰（牛市中顶背驰是明确看空信号）
            if p.divergence_type == "top":
                p.filter_pass = False
                p.filter_reason = "牛市排除: 顶背驰"
            elif p.zhongshu_position == "below":
                p.filter_pass = False
                p.filter_reason = "牛市排除: 中枢下方"

        elif market_state == "volatile":
            # 排除: 顶背驰 OR 中枢下方
            if p.divergence_type == "top":
                p.filter_pass = False
                p.filter_reason = "震荡排除: 顶背驰"
            elif p.zhongshu_position == "below":
                p.filter_pass = False
                p.filter_reason = "震荡排除: 中枢下方"

        elif market_state == "weak":
            # 偏弱市场: 评分制 — 综合中枢位置、MA、背驰打分，≥2通过
            weak_score = 0
            if p.zhongshu_position == "above":
                weak_score += 2
            elif p.zhongshu_position == "inside":
                weak_score += 1
            if p.ma_arrangement == "bull":
                weak_score += 2
            elif p.ma_arrangement == "mixed":
                weak_score += 1
            if p.divergence_type == "bottom":
                weak_score += 1
            if p.divergence_type == "top":
                weak_score -= 1
            if weak_score < 2:
                p.filter_pass = False
                p.filter_reason = f"偏弱排除: 得分={weak_score}<2"

    passed = sum(1 for p in patterns.values() if p.filter_pass)
    total = len(patterns)
    logger.info(f"L2形态过滤 [{market_state}]: {passed}/{total} 通过")

    return patterns


def get_pattern_bonus(p: PatternClassification, market_state: str = "volatile") -> float:
    """
    将形态分类转为评分修正量（±15范围），替代硬过滤。

    正分 = 形态有利，负分 = 形态不利。
    修正量叠加到 L2 Trend+Alpha+Vol+Fund 四维评分上。
    """
    bonus = 0.0

    # 中枢位置 (-3 ~ +5)
    if p.zhongshu_position == "above":
        bonus += 5
    elif p.zhongshu_position == "inside":
        bonus += 2
    elif p.zhongshu_position == "below":
        bonus -= 3

    # MA排列 (-4 ~ +4)
    if p.ma_arrangement == "bull":
        bonus += 4
    elif p.ma_arrangement == "mixed":
        bonus += 1
    elif p.ma_arrangement == "bear":
        bonus -= 4

    # 背驰 (-8 ~ +4)
    if p.divergence_type == "bottom":
        bonus += 4
    elif p.divergence_type == "top":
        bonus -= 8

    # 笔方向 (-2 ~ +2)
    if p.stroke_direction == "up":
        bonus += 2
    elif p.stroke_direction == "down":
        bonus -= 2

    # 形态清晰度作为缩放因子 (0.5x ~ 1.2x)
    quality_factor = 0.5 + p.pattern_quality / 10 * 0.7
    bonus = bonus * quality_factor

    return round(bonus, 1)


def get_pattern_priority_order(
    patterns: dict[str, PatternClassification],
    market_state: str = "volatile",
) -> list[str]:
    """
    按形态优先级排序（用于候选板块排名）。

    优先级: 中枢上方+底背驰+笔向上+MA多头 > 中枢上方+MA多头 > 其他
    """
    def _priority(p: PatternClassification) -> int:
        score = 0
        if p.zhongshu_position == "above":
            score += 3
        elif p.zhongshu_position == "inside":
            score += 1
        if p.divergence_type == "bottom":
            score += 2
        if p.stroke_direction == "up":
            score += 2
        if p.ma_arrangement == "bull":
            score += 2
        elif p.ma_arrangement == "bear":
            score -= 1
        return score

    sorted_names = sorted(patterns.keys(), key=lambda n: _priority(patterns[n]), reverse=True)
    return sorted_names
