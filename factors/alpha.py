"""相对大盘Alpha计算"""

import pandas as pd
import numpy as np


def calc_cumulative_return(close: pd.Series) -> pd.Series:
    """计算累计收益率"""
    return close / close.iloc[0] - 1


def calc_alpha(stock_close: pd.Series, benchmark_close: pd.Series,
               lookback: int = 20) -> pd.Series:
    """
    计算个股相对基准的Alpha（超额收益）。

    方法: 股票累计收益 - 基准累计收益
    """
    if len(stock_close) < lookback or len(benchmark_close) < lookback:
        return pd.Series(np.nan, index=stock_close.index)

    stock_ret = stock_close.pct_change()
    bench_ret = benchmark_close.pct_change()

    alpha = pd.Series(np.nan, index=stock_close.index)
    for i in range(lookback, len(stock_close)):
        stock_cum = (1 + stock_ret.iloc[i - lookback + 1:i + 1]).prod() - 1
        bench_cum = (1 + bench_ret.iloc[i - lookback + 1:i + 1]).prod() - 1
        alpha.iloc[i] = stock_cum - bench_cum

    return alpha


def calc_relative_strength(stock_close: pd.Series, benchmark_close: pd.Series,
                           lookback: int = 4) -> float:
    """
    计算个股相对基准的走势强弱。

    返回: 超额的标准化值, >0 表示跑赢基准
    """
    if len(stock_close) < lookback or len(benchmark_close) < lookback:
        return 0.0

    stock_ret = stock_close.tail(lookback).pct_change().dropna()
    bench_ret = benchmark_close.tail(lookback).pct_change().dropna()

    if len(stock_ret) == 0:
        return 0.0

    stock_cum = (1 + stock_ret).prod() - 1
    bench_cum = (1 + bench_ret).prod() - 1

    return float(stock_cum - bench_cum)


def is_outperforming(stock_close: pd.Series, benchmark_close: pd.Series,
                     lookback: int = 4) -> bool:
    """股票是否跑赢基准"""
    return calc_relative_strength(stock_close, benchmark_close, lookback) > 0


def calc_alpha_series(stock_close: pd.Series, benchmark_close: pd.Series,
                      period: int = 4) -> pd.Series:
    """
    计算Alpha序列（滚动窗口）。

    返回与stock_close同索引的Alpha序列
    """
    result = pd.Series(np.nan, index=stock_close.index)
    stock_ret = stock_close.pct_change()
    bench_ret = benchmark_close.pct_change()

    for i in range(period, len(stock_close)):
        stock_cum = (1 + stock_ret.iloc[i - period + 1:i + 1]).prod() - 1
        bench_cum = (1 + bench_ret.iloc[i - period + 1:i + 1]).prod() - 1
        result.iloc[i] = (stock_cum - bench_cum) * 100  # 百分比化

    return result
