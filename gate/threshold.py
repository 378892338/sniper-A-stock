"""动态阈值计算"""

import numpy as np
import pandas as pd

from core.logger import get_logger

logger = get_logger("gate.threshold")


def default_top_k(total: int, ratio: float = 0.30, min_k: int = 3) -> int:
    """默认TopK: max(min_k, total * ratio)"""
    return max(min_k, int(total * ratio))


def percentile_threshold(values: list[float], percentile: float) -> float:
    """分位数阈值"""
    if not values:
        return 0.0
    return float(np.percentile(values, percentile))


def calc_adaptive_ratio(historical_ratios: list[float],
                        target_avg: float = 0.30) -> float:
    """
    基于历史数据自适应调整比例。

    historical_ratios: 历史上各期的候选占比
    返回: 调整后的比例（不超0.50，不低于0.15）
    """
    if not historical_ratios:
        return target_avg

    avg = np.mean(historical_ratios)
    if avg > 0.5:
        return min(0.50, target_avg * 0.8)
    elif avg < 0.1:
        return max(0.15, target_avg * 1.5)
    return target_avg


def optimal_threshold_from_backtest(
    thresholds: list[float],
    win_rates: list[float],
    drawdowns: list[float],
    weight_win: float = 0.6,
    weight_dd: float = 0.4,
) -> float:
    """
    从回测结果反推最优阈值。

    thresholds: 各种候选阈值
    win_rates: 对应的胜率
    drawdowns: 对应的最大回撤（取绝对值）
    返回: 最优阈值
    """
    if len(thresholds) < 2:
        return thresholds[0] if thresholds else 0.3

    # 归一化
    wr_norm = np.array(win_rates) / max(win_rates) if max(win_rates) > 0 else np.ones(len(win_rates))
    dd_norm = 1 - (np.array(drawdowns) / max(drawdowns)) if max(drawdowns) > 0 else np.ones(len(win_rates))

    scores = wr_norm * weight_win + dd_norm * weight_dd
    best_idx = int(np.argmax(scores))
    logger.info(f"最优阈值: {thresholds[best_idx]:.2f} (score={scores[best_idx]:.3f})")
    return thresholds[best_idx]
