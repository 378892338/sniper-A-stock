"""MACD因子计算 — 金叉/死叉/背驰判断"""

import numpy as np
import pandas as pd


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均"""
    return series.ewm(span=period, adjust=False).mean()


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
              ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """计算MACD，返回 (DIF, DEA, HIST)"""
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    dif = ema_fast - ema_slow
    dea = calc_ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def is_golden_cross(dif: pd.Series, dea: pd.Series) -> pd.Series:
    """金叉: DIF上穿DEA（昨天DIF<=DEA, 今天DIF>DEA）"""
    prev_dif_above = dif.shift(1) > dea.shift(1)
    curr_dif_above = dif > dea
    return (~prev_dif_above) & curr_dif_above


def is_death_cross(dif: pd.Series, dea: pd.Series) -> pd.Series:
    """死叉: DIF下穿DEA（昨天DIF>=DEA, 今天DIF<DEA）"""
    prev_dif_below = dif.shift(1) < dea.shift(1)
    curr_dif_below = dif < dea
    return (~prev_dif_below) & curr_dif_below


def is_dif_above_dea(dif: pd.Series, dea: pd.Series) -> pd.Series:
    """DIF > DEA"""
    return dif > dea


def is_hist_positive(hist: pd.Series) -> pd.Series:
    """柱状图为正"""
    return hist > 0


def is_dif_above_zero(dif: pd.Series) -> pd.Series:
    """DIF在零轴上方"""
    return dif > 0


def is_dif_turning_up(dif: pd.Series) -> pd.Series:
    """DIF拐头向上（今天 > 昨天 且 昨天 < 前天）"""
    return (dif > dif.shift(1)) & (dif.shift(1) < dif.shift(2))


def calc_macd_area(hist: pd.Series, start_idx: int, end_idx: int) -> float:
    """计算MACD柱状图面积（用于背驰比较）"""
    segment = hist.iloc[start_idx:end_idx + 1]
    return float(segment.abs().sum())


def detect_top_divergence(close: pd.Series, dif: pd.Series, hist: pd.Series,
                          window: int = 60) -> pd.Series:
    """
    检测顶背驰：价格创新高，但MACD面积缩小。

    已弃用：请改用 factors.chanlun.divergence.detect_top_divergence (分型法更准确)。
    此处保留作为向后兼容的包装。
    """
    import warnings
    warnings.warn(
        "factors.macd.detect_top_divergence 已弃用，"
        "请使用 factors.chanlun.divergence.detect_top_divergence",
        DeprecationWarning, stacklevel=2,
    )
    from factors.chanlun.divergence import detect_top_divergence as _chanlun_detect
    df = pd.DataFrame({"close": close, "high": close, "low": close})
    return _chanlun_detect(df, dif, hist, window)


def check_daily_macd_above_ma20(close: pd.Series) -> dict:
    """日线MACD金叉 + 收盘价站上20日均线，返回 {macd_ok, ma_ok, all_ok, dif, dea, hist}"""
    ma20 = close.rolling(20).mean()
    dif, dea, hist = calc_macd(close)
    macd_ok = bool((dif > dea).iloc[-1])
    ma_ok = bool(close.iloc[-1] > ma20.iloc[-1]) if not pd.isna(ma20.iloc[-1]) else True
    return {"macd_ok": macd_ok, "ma_ok": ma_ok, "all_ok": macd_ok and ma_ok,
            "dif": dif, "dea": dea, "hist": hist}


def _find_local_maxima(series: pd.Series) -> list[int]:
    """找局部极大值的索引列表"""
    indices = []
    for i in range(1, len(series) - 1):
        if series.iloc[i] > series.iloc[i - 1] and series.iloc[i] > series.iloc[i + 1]:
            indices.append(i)
    return indices
