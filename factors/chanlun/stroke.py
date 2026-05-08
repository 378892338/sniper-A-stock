"""笔的划分 — 缠论 Sprint 1"""

import pandas as pd
import numpy as np


def divide_strokes(df: pd.DataFrame, min_bars: int = 3) -> pd.DataFrame:
    """
    划分笔：顶分型与底分型之间的连线。

    笔的规则：
    1. 顶分型和底分型之间至少间隔 min_bars 根K线（含两端）
    2. 顶分型后必须连接底分型，反之亦然
    3. 笔的起点是底/顶分型的最低/最高点

    返回: DataFrame添加列 ['stroke', 'stroke_type', 'stroke_idx']
        stroke: 该K线是否属于某笔
        stroke_type: 1=向上笔, -1=向下笔, 0=不确定
        stroke_idx: 笔的编号
    """
    from factors.chanlun.fractal import identify_fractals, filter_valid_fractals

    df_with_frac = filter_valid_fractals(df)
    result = df_with_frac.copy()
    result["stroke"] = True
    result["stroke_type"] = 0
    result["stroke_idx"] = -1

    # 收集有效分型位置
    top_positions = result.index[result["top_fractal"]].tolist()
    bottom_positions = result.index[result["bottom_fractal"]].tolist()

    if len(top_positions) == 0 or len(bottom_positions) == 0:
        return result

    # 找第一个分型
    all_fractals = sorted(
        [(p, "top", result.loc[p, "high"]) for p in top_positions] +
        [(p, "bottom", result.loc[p, "low"]) for p in bottom_positions],
        key=lambda x: x[0]
    )

    if len(all_fractals) < 2:
        return result

    stroke_idx = 0
    prev = all_fractals[0]

    for i in range(1, len(all_fractals)):
        cur = all_fractals[i]
        pos_diff = result.index.get_loc(cur[0]) - result.index.get_loc(prev[0])

        # 笔至少间隔 min_bars 根K线
        if pos_diff < min_bars:
            continue

        # 类型必须交替
        if prev[1] == cur[1]:
            # 同类型 → 顶取更高，底取更低
            if cur[1] == "top" and cur[2] > prev[2]:
                prev = cur
            elif cur[1] == "bottom" and cur[2] < prev[2]:
                prev = cur
            continue

        # 有效笔
        start_iloc = result.index.get_loc(prev[0])
        end_iloc = result.index.get_loc(cur[0])

        stroke_type = 1 if prev[1] == "bottom" else -1
        result.iloc[start_iloc:end_iloc + 1, result.columns.get_loc("stroke_type")] = stroke_type
        result.iloc[start_iloc:end_iloc + 1, result.columns.get_loc("stroke_idx")] = stroke_idx

        stroke_idx += 1
        prev = cur

    return result


def get_stroke_points(df: pd.DataFrame) -> list[dict]:
    """获取笔的端点列表 [{idx, type, price}]"""
    strokes = divide_strokes(df)
    points = []
    prev_idx = -1
    for i in range(len(strokes)):
        sidx = strokes["stroke_idx"].iloc[i]
        if sidx != prev_idx and sidx >= 0:
            stype = strokes["stroke_type"].iloc[i]
            price = strokes["low"].iloc[i] if stype == 1 else strokes["high"].iloc[i]
            points.append({
                "idx": i,
                "date": strokes.index[i],
                "type": "底" if stype == 1 else "顶",
                "price": float(price),
                "stroke_idx": int(sidx),
            })
            prev_idx = sidx
    return points
