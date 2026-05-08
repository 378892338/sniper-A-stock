"""背驰判断 — 缠论 Sprint 1 (MACD面积比较)"""

import pandas as pd
import numpy as np


def compare_macd_area(hist: pd.Series, stroke_start: int, stroke_end: int,
                      ref_start: int, ref_end: int) -> float:
    """
    比较两段走势的MACD面积。

    返回: 当前面积 / 参考面积。 < 1 表示当前力度减弱。
    """
    cur_area = float(hist.iloc[stroke_start:stroke_end + 1].abs().sum())
    ref_area = float(hist.iloc[ref_start:ref_end + 1].abs().sum())

    if ref_area == 0:
        return 1.0
    return cur_area / ref_area


def detect_top_divergence(df: pd.DataFrame, hist: pd.Series,
                          window: int = 60) -> pd.Series:
    """
    检测顶背驰：
    - 价格创新高
    - 但MACD柱状图面积较前一段同向走势缩小
    - 返回bool序列
    """
    from factors.chanlun.fractal import identify_fractals

    result = pd.Series(False, index=df.index)

    if "top_fractal" not in df.columns:
        df = identify_fractals(df)

    top_positions = df.index[df["top_fractal"]].tolist()
    if len(top_positions) < 2:
        return result

    for i in range(1, len(top_positions)):
        cur_top = top_positions[i]
        prev_top = top_positions[i - 1]

        cur_idx = df.index.get_loc(cur_top)
        prev_idx = df.index.get_loc(prev_top)

        if cur_idx - prev_idx > window:
            continue

        # 价格创新高
        if df["high"].iloc[cur_idx] <= df["high"].iloc[prev_idx]:
            continue

        # MACD面积缩小
        cur_area = float(hist.iloc[prev_idx:cur_idx + 1].abs().sum())
        # 参考前一段的面积
        ref_start = max(0, prev_idx - (cur_idx - prev_idx))
        ref_area = float(hist.iloc[ref_start:prev_idx + 1].abs().sum())

        if ref_area > 0 and cur_area < ref_area * 0.8:
            result.iloc[cur_idx] = True

    return result


def detect_bottom_divergence(df: pd.DataFrame, hist: pd.Series,
                             window: int = 60) -> pd.Series:
    """
    检测底背驰：
    - 价格创新低
    - 但MACD柱状图面积较前一段同向走势缩小
    - 返回bool序列
    """
    from factors.chanlun.fractal import identify_fractals

    result = pd.Series(False, index=df.index)

    if "bottom_fractal" not in df.columns:
        df = identify_fractals(df)

    bottom_positions = df.index[df["bottom_fractal"]].tolist()
    if len(bottom_positions) < 2:
        return result

    for i in range(1, len(bottom_positions)):
        cur_bot = bottom_positions[i]
        prev_bot = bottom_positions[i - 1]

        cur_idx = df.index.get_loc(cur_bot)
        prev_idx = df.index.get_loc(prev_bot)

        if cur_idx - prev_idx > window:
            continue

        # 价格创新低
        if df["low"].iloc[cur_idx] >= df["low"].iloc[prev_idx]:
            continue

        # MACD面积缩小
        cur_area = float(hist.iloc[prev_idx:cur_idx + 1].abs().sum())
        ref_start = max(0, prev_idx - (cur_idx - prev_idx))
        ref_area = float(hist.iloc[ref_start:prev_idx + 1].abs().sum())

        if ref_area > 0 and cur_area < ref_area * 0.8:
            result.iloc[cur_idx] = True

    return result


def check_monthly_bottom_divergence(monthly_df: pd.DataFrame, monthly_dif: pd.Series,
                                   lookback: int = 36, trough_window: int = 3) -> bool:
    """检查月线级别是否存在底背离。价格创新低但DIF走高。"""
    if len(monthly_dif) < lookback or "low" not in monthly_df.columns:
        return False
    dif_tail = monthly_dif.tail(lookback).reset_index(drop=True)
    low_tail = monthly_df["low"].tail(lookback).reset_index(drop=True)
    n = len(dif_tail)
    troughs = []
    for i in range(trough_window, n - trough_window):
        if all(dif_tail.iloc[i] < dif_tail.iloc[i - j] for j in range(1, trough_window + 1)) and \
           all(dif_tail.iloc[i] < dif_tail.iloc[i + j] for j in range(1, trough_window + 1)):
            troughs.append(i)
    if len(troughs) < 2:
        return False
    prev, curr = troughs[-2], troughs[-1]
    return float(low_tail.iloc[curr]) < float(low_tail.iloc[prev]) and \
           float(dif_tail.iloc[curr]) > float(dif_tail.iloc[prev])


def check_weekly_top_divergence(weekly_df: pd.DataFrame,
                                weekly_hist: pd.Series) -> bool:
    """检查周线级别是否存在顶背驰"""
    signals = detect_top_divergence(weekly_df, weekly_hist)
    recent = signals.tail(4)
    return bool(recent.any())


def check_weekly_bottom_divergence(weekly_df: pd.DataFrame,
                                    weekly_hist: pd.Series) -> bool:
    """检查周线级别是否存在底背驰"""
    signals = detect_bottom_divergence(weekly_df, weekly_hist)
    recent = signals.tail(4)
    return bool(recent.any())


def check_daily_top_divergence(daily_df: pd.DataFrame,
                               daily_hist: pd.Series) -> bool:
    """检查日线级别是否存在顶背驰"""
    signals = detect_top_divergence(daily_df, daily_hist)
    recent = signals.tail(5)
    return bool(recent.any())


def check_daily_bottom_divergence(daily_df: pd.DataFrame,
                                  daily_hist: pd.Series) -> bool:
    """检查日线级别是否存在底背驰"""
    signals = detect_bottom_divergence(daily_df, daily_hist)
    recent = signals.tail(5)
    return bool(recent.any())
