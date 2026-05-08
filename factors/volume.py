"""量能分析 — 放量/缩量/量价关系 + 三部分量价健康度评估"""

import pandas as pd
import numpy as np


def calc_volume_ma(volume: pd.Series, period: int = 20) -> pd.Series:
    """成交量均线"""
    return volume.rolling(window=period).mean()


def calc_volume_ratio(volume: pd.Series, period: int = 5) -> pd.Series:
    """量比: 当日量 / 前 period 日均量"""
    return volume / volume.shift(1).rolling(window=period).mean()


def is_volume_expanding(volume: pd.Series, period: int = 5, threshold: float = 1.3) -> pd.Series:
    """放量: 量比 > threshold"""
    ratio = calc_volume_ratio(volume, period)
    return ratio > threshold


def is_volume_shrinking(volume: pd.Series, period: int = 5) -> pd.Series:
    """缩量: 量比 < 1"""
    ratio = calc_volume_ratio(volume, period)
    return ratio < 1.0


def classify_volume_price(volume: pd.Series, close: pd.Series,
                          period: int = 5) -> pd.Series:
    """
    量价关系分类。

    返回字符串序列:
    - "放量上涨"  量>均量1.3倍, 价涨
    - "缩量上涨"  量<均量, 价涨
    - "放量下跌"  量>均量1.3倍, 价跌
    - "缩量下跌"  量<均量, 价跌
    - "放量滞涨"  量>均量1.3倍, 价平(-0.5%~+0.5%)
    - "正常"
    """
    ratio = calc_volume_ratio(volume, period)
    pct_chg = close.pct_change()

    result = pd.Series("正常", index=volume.index)

    expanding = ratio > 1.3
    shrinking = ratio < 1.0
    up = pct_chg > 0.005
    down = pct_chg < -0.005
    flat = (~up) & (~down)

    result[expanding & up] = "放量上涨"
    result[shrinking & up] = "缩量上涨"
    result[expanding & down] = "放量下跌"
    result[shrinking & down] = "缩量下跌"
    result[expanding & flat] = "放量滞涨"

    return result


def score_volume_health(volume: pd.Series, close: pd.Series,
                        period: int = 20) -> float:
    """
    量价健康度评分 (0-100) — 保留旧接口，内部委托到三部分法。

    上涨放量 > 缩量上涨 > 放量滞涨 > 缩量阴跌
    """
    return score_volume_three_part(volume, close, period)


def _detect_price_trend(close: pd.Series, period: int = 20) -> str:
    """检测价格趋势方向: "up" / "down" / "neutral" """
    if len(close) < period:
        return "neutral"
    ma_short = close.rolling(5).mean()
    ma_long = close.rolling(period).mean()
    if ma_short.iloc[-1] > ma_long.iloc[-1] * 1.02:
        return "up"
    elif ma_short.iloc[-1] < ma_long.iloc[-1] * 0.98:
        return "down"
    return "neutral"


def score_volume_three_part(volume: pd.Series, close: pd.Series,
                            period: int = 20, decay: float = 0.92) -> float:
    """
    三部分量价健康度评分 (0-100)。

    第一部分 (40分): 趋势感知的量价关系
        - 上涨趋势中放量上涨加分，缩量下跌不加分
        - 下跌趋势中缩量下跌加分，放量下跌减分
        - 带指数时间衰减 decay^t，最近数据权重更大

    第二部分 (30分): 成交量趋势方向
        - 成交量MA(5) vs MA(20)，量能持续放大加分

    第三部分 (30分): 涨跌量比
        - 上涨日平均量 / 下跌日平均量，量比 > 1.2 加分
    """
    if len(volume) < max(period, 5) or len(close) < max(period, 5):
        return 50.0

    vp = classify_volume_price(volume, close)
    trend = _detect_price_trend(close, period)
    recent_vp = vp.tail(period)
    recent_close = close.tail(period)

    # === 第一部分: 趋势感知的量价关系 (40分) ===
    part1 = 0.0
    max_part1 = 0.0
    n = len(recent_vp)
    for i, label in enumerate(recent_vp):
        t = n - 1 - i  # 0=最新, n-1=最旧
        w = decay ** t

        if trend == "up":
            if label == "放量上涨":
                part1 += 4 * w
            elif label == "缩量上涨":
                part1 += 1 * w
            elif label == "放量下跌":
                part1 -= 4 * w
            elif label == "缩量下跌":
                part1 += 0.5 * w
            elif label == "放量滞涨":
                part1 -= 1 * w
        elif trend == "down":
            if label == "放量上涨":
                part1 -= 1 * w
            elif label == "缩量上涨":
                part1 -= 1 * w
            elif label == "放量下跌":
                part1 -= 3 * w
            elif label == "缩量下跌":
                part1 += 2 * w
            elif label == "放量滞涨":
                part1 -= 2 * w
        else:
            if label == "放量上涨":
                part1 += 3 * w
            elif label == "缩量上涨":
                part1 += 1 * w
            elif label == "放量下跌":
                part1 -= 3 * w
            elif label == "缩量下跌":
                part1 -= 1 * w
            elif label == "放量滞涨":
                part1 -= 2 * w
        max_part1 += 4 * w

    if max_part1 > 0:
        part1_score = 20 + (part1 / max_part1) * 20
    else:
        part1_score = 20
    part1_score = max(0.0, min(40.0, part1_score))

    # === 第二部分: 成交量趋势方向 (30分) ===
    vol_ma_short = volume.rolling(5).mean()
    vol_ma_long = volume.rolling(period).mean()
    vol_trend_ratio = vol_ma_short.iloc[-1] / vol_ma_long.iloc[-1] if vol_ma_long.iloc[-1] > 0 else 1.0
    vol_slope = (vol_ma_short.iloc[-1] / vol_ma_short.iloc[-min(5, len(vol_ma_short))] - 1) if len(vol_ma_short) >= 5 else 0
    part2_score = 15 + (vol_trend_ratio - 1.0) * 30 + vol_slope * 20
    part2_score = max(0.0, min(30.0, part2_score))

    # === 第三部分: 涨跌量比 (30分) ===
    pct_chg = recent_close.pct_change()
    up_days = pct_chg > 0.002
    down_days = pct_chg < -0.002
    up_vol = volume.tail(period)[up_days.values].mean()
    down_vol = volume.tail(period)[down_days.values].mean()
    if pd.isna(up_vol):
        up_vol = 0
    if pd.isna(down_vol):
        down_vol = volume.tail(period).mean()
    vol_ratio = up_vol / down_vol if down_vol > 0 else 1.0
    part3_score = 15 + (vol_ratio - 1.0) * 15
    part3_score = max(0.0, min(30.0, part3_score))

    total = part1_score + part2_score + part3_score
    return round(max(0.0, min(100.0, total)), 1)


def is_volume_healthy_for_gate(volume: pd.Series, close: pd.Series,
                               period: int = 20, threshold: float = 50.0) -> bool:
    """Gate 门控用: 量价是否健康"""
    return score_volume_three_part(volume, close, period) >= threshold


def calc_amount_ma(amount: pd.Series, period: int = 20) -> pd.Series:
    """成交额均线"""
    return amount.rolling(window=period).mean()


def is_amount_trending_up(amount: pd.Series, short: int = 5, long: int = 20) -> bool:
    """成交额趋势向上: 短期均线 > 长期均线"""
    if len(amount) < long:
        return False
    ma_short = calc_amount_ma(amount, short)
    ma_long = calc_amount_ma(amount, long)
    return float(ma_short.iloc[-1]) > float(ma_long.iloc[-1])
