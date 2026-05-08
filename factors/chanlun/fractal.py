"""顶底分型识别 — 缠论 Sprint 1"""

import pandas as pd
import numpy as np


def identify_fractals(df: pd.DataFrame) -> pd.DataFrame:
    """
    识别顶分型和底分型。

    顶分型：中间K线最高>左、右K线最高（同时中间K线最低>左、右K线最低？无此要求，只看高）
    底分型：中间K线最低<左、右K线最低

    返回DataFrame添加列: ['top_fractal', 'bottom_fractal']
    """
    result = df.copy()
    result["top_fractal"] = False
    result["bottom_fractal"] = False

    if len(result) < 3:
        return result

    for i in range(1, len(result) - 1):
        cur = result.iloc[i]
        prev = result.iloc[i - 1]
        nxt = result.iloc[i + 1]

        # 顶分型：中间高 > 左右高
        if cur["high"] > prev["high"] and cur["high"] > nxt["high"]:
            result.iloc[i, result.columns.get_loc("top_fractal")] = True

        # 底分型：中间低 < 左右低
        if cur["low"] < prev["low"] and cur["low"] < nxt["low"]:
            result.iloc[i, result.columns.get_loc("bottom_fractal")] = True

    return result


def filter_valid_fractals(df: pd.DataFrame) -> pd.DataFrame:
    """
    过滤有效分型：顶分型后必须跟底分型，底分型后必须跟顶分型。
    同一类型连续出现时保留更极端的。
    """
    result = df.copy()
    if "top_fractal" not in result.columns or "bottom_fractal" not in result.columns:
        result = identify_fractals(result)

    last_type = None  # 'top' or 'bottom'
    last_idx = -1

    for i in range(len(result)):
        if result["top_fractal"].iloc[i]:
            if last_type == "top":
                # 两个顶分型 → 保留更高的
                if result["high"].iloc[i] > result["high"].iloc[last_idx]:
                    result.iloc[last_idx, result.columns.get_loc("top_fractal")] = False
                    last_idx = i
                else:
                    result.iloc[i, result.columns.get_loc("top_fractal")] = False
            else:
                last_type = "top"
                last_idx = i

        if result["bottom_fractal"].iloc[i]:
            if last_type == "bottom":
                # 两个底分型 → 保留更低的
                if result["low"].iloc[i] < result["low"].iloc[last_idx]:
                    result.iloc[last_idx, result.columns.get_loc("bottom_fractal")] = False
                    last_idx = i
                else:
                    result.iloc[i, result.columns.get_loc("bottom_fractal")] = False
            else:
                last_type = "bottom"
                last_idx = i

    return result
