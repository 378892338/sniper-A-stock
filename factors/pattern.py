"""形态识别 — 平台突破/均线粘合/底部反转"""

import pandas as pd
import numpy as np


def detect_platform_breakout(df: pd.DataFrame, lookback: int = 60,
                             max_amplitude: float = 0.25,
                             breakout_pct: float = 0.03,
                             vol_ratio: float = 1.5) -> pd.Series:
    """
    检测平台突破形态。

    条件：
    1. 过去 lookback 天振幅 < max_amplitude
    2. 当天涨幅 > breakout_pct
    3. 当天成交量 > 20日均量 × vol_ratio

    返回: bool序列，True=突破当天
    """
    result = pd.Series(False, index=df.index)
    if len(df) < lookback:
        return result

    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"] if "volume" in df.columns else pd.Series(1, index=df.index)

    vol_ma20 = vol.rolling(20).mean()

    for i in range(lookback, len(df)):
        segment_high = high.iloc[i - lookback:i]
        segment_low = low.iloc[i - lookback:i]
        amplitude = (segment_high.max() - segment_low.min()) / segment_low.min()

        if amplitude <= max_amplitude:
            pct_chg = (close.iloc[i] - close.iloc[i - 1]) / close.iloc[i - 1]
            if pct_chg >= breakout_pct:
                if vol.iloc[i] > vol_ma20.iloc[i] * vol_ratio:
                    result.iloc[i] = True

    return result


def detect_ma_convergence(df: pd.DataFrame, ma_list: list[int] = None,
                          converge_pct: float = 0.05,
                          min_converge_days: int = 15) -> pd.Series:
    """
    检测均线粘合形态。

    条件：多条均线在 converge_pct 范围内**连续**粘合 ≥ min_converge_days 天 (H5)

    返回: bool序列，True=粘合当天
    """
    if ma_list is None:
        ma_list = [5, 10, 20, 60]

    result = pd.Series(False, index=df.index)
    if len(df) < max(ma_list):
        return result

    close = df["close"]
    mas = {}
    for p in ma_list:
        mas[p] = close.rolling(p).mean()

    consecutive = 0
    for i in range(max(ma_list), len(df)):
        ma_values = [mas[p].iloc[i] for p in ma_list]
        ma_range = (max(ma_values) - min(ma_values)) / min(ma_values)
        if ma_range <= converge_pct:
            consecutive += 1
            if consecutive >= min_converge_days:
                result.iloc[i] = True
        else:
            consecutive = 0

    return result


def detect_double_bottom(df: pd.DataFrame, min_formation_days: int = 30,
                         gap_min: int = 20, bottom_tol: float = 0.03) -> pd.Series:
    """
    检测W底（双底反转）形态。

    条件：
    1. 形成周期 ≥ min_formation_days
    2. 两底间隔 ≥ gap_min 天
    3. 两底价格差异 < bottom_tol
    4. 中间有反弹（高点 > 两底均价 × 1.05）

    返回: bool序列，True=第二个底确认当天
    """
    result = pd.Series(False, index=df.index)
    if len(df) < min_formation_days:
        return result

    low = df["low"]
    high = df["high"]
    close = df["close"]

    for i in range(min_formation_days, len(df)):
        segment_low = low.iloc[i - min_formation_days:i]
        # 找两个最低点
        min_indices = segment_low.nsmallest(3).index
        if len(min_indices) < 2:
            continue

        sorted_mins = sorted(min_indices)
        idx1 = df.index.get_loc(sorted_mins[0]) if isinstance(sorted_mins[0], pd.Timestamp) else sorted_mins[0]
        idx2 = df.index.get_loc(sorted_mins[1]) if isinstance(sorted_mins[1], pd.Timestamp) else sorted_mins[1]

        if abs(idx2 - idx1) < gap_min:
            continue

        price1 = low.iloc[idx1]
        price2 = low.iloc[idx2]
        if abs(price1 - price2) / price1 > bottom_tol:
            continue

        # 中间有反弹
        mid_high = high.iloc[min(idx1, idx2):max(idx1, idx2)].max()
        avg_bottom = (price1 + price2) / 2
        if mid_high > avg_bottom * 1.05:
            result.iloc[idx2] = True

    return result


def detect_flag_triangle_breakout(df: pd.DataFrame,
                                  min_flagpole_pct: float = 0.10,
                                  lookback: int = 30) -> pd.Series:
    """
    检测旗形/三角突破。

    条件：
    1. 前一段有明显涨幅（旗杆）> min_flagpole_pct
    2. 之后横盘整理（振幅缩小）
    3. 突破整理区间上沿

    返回: bool序列
    """
    result = pd.Series(False, index=df.index)
    close = df["close"]
    if len(df) < lookback:
        return result

    for i in range(lookback + 20, len(df)):
        # 前 lookback 天的最大涨幅
        past_close = close.iloc[i - lookback - 20:i - 20]
        if len(past_close) < 10:
            continue
        pole_ret = (past_close.iloc[-1] - past_close.iloc[0]) / past_close.iloc[0]

        if pole_ret >= min_flagpole_pct:
            # 最近20天振幅
            recent = df.iloc[i - 20:i]
            amplitude = (recent["high"].max() - recent["low"].min()) / recent["low"].min()

            if amplitude < 0.15:
                if close.iloc[i] > recent["high"].max():
                    result.iloc[i] = True

    return result


def detect_limit_up_breakout(df: pd.DataFrame, lookback: int = 60,
                              vol_ratio: float = 1.5) -> pd.Series:
    """
    检测涨停突破形态。

    条件：
    1. 当日涨幅 >= 9.5%（涨停）
    2. 当日成交量 > 20日均量 × vol_ratio
    3. 收盘价突破 lookback 日最高点

    返回: bool序列，True=涨停突破当天
    """
    result = pd.Series(False, index=df.index)
    if len(df) < lookback:
        return result

    close = df["close"]
    vol = df["volume"] if "volume" in df.columns else pd.Series(1, index=df.index)
    vol_ma20 = vol.rolling(20).mean()
    high_lookback = close.rolling(lookback).max().shift(1)

    for i in range(lookback, len(df)):
        pct_chg = (close.iloc[i] - close.iloc[i - 1]) / close.iloc[i - 1]
        if pct_chg < 0.095:
            continue
        if vol.iloc[i] < vol_ma20.iloc[i] * vol_ratio:
            continue
        if pd.isna(high_lookback.iloc[i]):
            continue
        if close.iloc[i] > high_lookback.iloc[i]:
            result.iloc[i] = True

    return result


def score_breakout_patterns(df: pd.DataFrame) -> dict[str, float]:
    """
    综合形态评分，每项 0-1，只取 top-2 计入最终得分。

    返回: {形态名: 0-1得分}
    """
    scores = {}

    # 涨停突破
    lu = detect_limit_up_breakout(df)
    scores["涨停突破"] = min(1.0, lu.tail(60).sum() / 2)

    platform = detect_platform_breakout(df)
    scores["平台突破"] = min(1.0, platform.tail(20).sum() / 3)

    ma_conv = detect_ma_convergence(df)
    scores["均线粘合"] = min(1.0, ma_conv.tail(20).sum() / 5)

    db = detect_double_bottom(df)
    scores["W底"] = min(1.0, db.tail(30).sum() / 2)

    flag = detect_flag_triangle_breakout(df)
    scores["旗形突破"] = min(1.0, flag.tail(20).sum() / 2)

    return scores


def calc_pattern_bonus(df: pd.DataFrame) -> tuple[float, dict[str, float]]:
    """
    计算 L3 形态加成 (0-25 分)。

    三部分:
    - 涨停突破 (0-8): 20天内=8, 60天内=5, 无=0
    - 经典形态 (0-7): 取 top-2 最强形态，映射到 0-7
    - 缠论结构 (0-10): 由 chanlun buy_points 提供，此处返回占位

    返回: (total_bonus, detail_dict)
    """
    lu = detect_limit_up_breakout(df)
    lu_20d = lu.tail(20).any()
    lu_60d = lu.tail(60).any()

    if lu_20d:
        lu_score = 8.0
    elif lu_60d:
        lu_score = 5.0
    else:
        lu_score = 0.0

    # 经典形态: 取 top-2，缩放到 0-7
    pattern_scores = score_breakout_patterns(df)
    # 排除涨停突破（单独计分），只取 4 个经典形态
    classic = {k: v for k, v in pattern_scores.items() if k != "涨停突破"}
    top2 = sorted(classic.values(), reverse=True)[:2]
    classic_score = sum(top2) / 2 * 7  # 两个满分=7, 一个满分=3.5

    total = lu_score + classic_score
    detail = {
        "涨停突破": lu_score,
        "经典形态": round(classic_score, 1),
        "缠论结构": 0.0,  # 由 chanlun 模块填充
        "形态总分": round(total, 1),
    }
    return round(min(25.0, total), 1), detail
