"""K线包含处理 — 缠论基础模块 Sprint 1"""

import pandas as pd
import numpy as np


def process_containment(df: pd.DataFrame) -> pd.DataFrame:
    """
    K线包含处理。

    规则：
    - 上升趋势中（前一根K线低点更高）→ 取高高（high取max, low取max）
    - 下降趋势中（前一根K线低点更低）→ 取低低（high取min, low取min）

    返回: 包含处理后的DataFrame，保留原始K线数量（被包含的K线标记为drop）
    """
    if len(df) < 2:
        df = df.copy()
        df["direction"] = 0
        df["merged"] = False
        return df

    result = df.copy()
    result["direction"] = 0  # 0=起始, 1=向上, -1=向下
    result["merged"] = False  # 是否被合并到前一根

    i = 1
    while i < len(result):
        cur = result.iloc[i]
        prev = result.iloc[i - 1]

        # 判断当前趋势方向
        if cur["low"] > prev["low"] and cur["high"] > prev["high"]:
            direction = 1  # 向上
        elif cur["low"] < prev["low"] and cur["high"] < prev["high"]:
            direction = -1  # 向下
        else:
            # 包含关系
            if cur["high"] <= prev["high"] and cur["low"] >= prev["low"]:
                # 当前被前一根包含
                prev_dir = result["direction"].iloc[i - 1]
                if prev_dir >= 0:  # 向上 → 取高高
                    result.iloc[i - 1, result.columns.get_loc("high")] = max(prev["high"], cur["high"])
                    result.iloc[i - 1, result.columns.get_loc("low")] = max(prev["low"], cur["low"])
                else:  # 向下 → 取低低
                    result.iloc[i - 1, result.columns.get_loc("high")] = min(prev["high"], cur["high"])
                    result.iloc[i - 1, result.columns.get_loc("low")] = min(prev["low"], cur["low"])
                result.iloc[i, result.columns.get_loc("merged")] = True
                result.iloc[i, result.columns.get_loc("direction")] = result["direction"].iloc[i - 1]
                i += 1
                continue
            elif cur["high"] >= prev["high"] and cur["low"] <= prev["low"]:
                # 当前包含前一根
                prev_dir = result["direction"].iloc[i - 1]
                if prev_dir >= 0:  # 向上 → 取高高
                    result.iloc[i - 1, result.columns.get_loc("high")] = max(prev["high"], cur["high"])
                    result.iloc[i - 1, result.columns.get_loc("low")] = max(prev["low"], cur["low"])
                else:  # 向下 → 取低低
                    result.iloc[i - 1, result.columns.get_loc("high")] = min(prev["high"], cur["high"])
                    result.iloc[i - 1, result.columns.get_loc("low")] = min(prev["low"], cur["low"])
                # 当前K线变成合并后的值
                for col in ["open", "close", "high", "low"]:
                    result.iloc[i, result.columns.get_loc(col)] = result.iloc[i - 1, result.columns.get_loc(col)]
                result.iloc[i, result.columns.get_loc("direction")] = result["direction"].iloc[i - 1]
                i += 1
                continue

            # 不包含 → 判断方向
            if cur["high"] > prev["high"]:
                direction = 1
            else:
                direction = -1

        result.iloc[i, result.columns.get_loc("direction")] = direction
        i += 1

    return result


def get_merged_bars(df: pd.DataFrame) -> pd.DataFrame:
    """获取包含处理后的独立K线（去除被合并的）"""
    processed = process_containment(df)
    return processed[~processed["merged"]].drop(columns=["merged"]).copy()
