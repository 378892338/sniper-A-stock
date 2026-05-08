"""滚动指标预计算 — 一次性算完全部因子的全时间序列

替代方案：不再每月对每只股票重复 compute_raw_factors，
而是每只股票一次性算完全部日期的因子值，按月查表。

用法:
  from factors.precompute import precompute_stock_all_factors
  pre = precompute_stock_all_factors(daily, weekly)
  val_at_month_end = pre.loc[month_end, "momentum_12_1"]
"""

import numpy as np
import pandas as pd

from core.logger import get_logger
from factors.macd import calc_macd
from gate.layer2_sector import _score_trend_5dim

logger = get_logger("factors.precompute")


def _rolling_bottom_divergence(
    daily_df: pd.DataFrame, daily_hist: pd.Series
) -> pd.Series:
    """预计算每日的底背驰信号 (0或1)。

    使用 detect_bottom_divergence() 一次性找出全部背驰点，
    然后前向填充：当天往后5天内出现背驰 → 1.0
    """
    from factors.chanlun.divergence import detect_bottom_divergence

    idx = daily_df.index
    result = pd.Series(0.0, index=idx)

    if len(daily_df) < 60:
        return result

    try:
        signals = detect_bottom_divergence(daily_df, daily_hist)
        # 仅在信号确认当日标记，不回溯（避免前向传播偏差）
        for div_date in signals.index[signals]:
            div_idx = signals.index.get_loc(div_date)
            result.iloc[div_idx] = 1.0
    except Exception as e:
        logger.warning(f"底背驰预计算失败: {e}")

    return result


def _rolling_volume_reversal(
    volume: pd.Series, close: pd.Series, period: int = 10
) -> pd.Series:
    """预计算每日的量能反转得分 (0-1)，向量化实现"""
    idx = volume.index
    result = pd.Series(0.5, index=idx)
    n = len(volume)

    if n < period + 5:
        return result

    pct = close.pct_change()

    # 用滚动窗口计算上涨/下跌日的平均量
    up_vol = volume * (pct > 0.002).astype(float)
    down_vol = volume * (pct < -0.002).astype(float)
    up_count = (pct > 0.002).rolling(period).sum().replace(0, np.nan)
    down_count = (pct < -0.002).rolling(period).sum().replace(0, np.nan)

    up_mean = up_vol.rolling(period).sum() / up_count
    down_mean = down_vol.rolling(period).sum() / down_count

    ratio = down_mean / up_mean.replace(0, np.nan)
    mask = (up_mean.notna() & down_mean.notna() & (down_mean > 0))
    result[mask] = (1.5 - ratio[mask]).clip(0, 1) / 0.9

    return result.round(3)


def _rolling_bottom_fractal(daily_df: pd.DataFrame) -> pd.Series:
    """预计算每日的底分型得分 (0或1)。

    使用 identify_fractals() 一次性标记全部分型。
    """
    from factors.chanlun.fractal import identify_fractals

    idx = daily_df.index
    result = pd.Series(0.0, index=idx)

    if len(daily_df) < 16:
        return result

    try:
        df = identify_fractals(daily_df.copy())
        if "bottom_fractal" in df.columns:
            result = df["bottom_fractal"].astype(float)
    except Exception as e:
        logger.warning(f"底分型预计算失败: {e}")

    return result


def precompute_stock_all_factors(
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame | None = None,
    _monthly_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """一次性预计算某只股票全部原始因子的全时间序列。

    参数:
        daily_df: 日线 DataFrame (必须含 close, high, low, 可选 amount/volume)
        weekly_df: 周线 DataFrame (用于 MACD 趋势)
        _monthly_df: 月线 (暂未使用，保留接口兼容)

    返回:
        DataFrame, 索引与 daily_df 一致，列为各因子名。
    """
    close = daily_df["close"]
    high = daily_df["high"]
    low = daily_df["low"]
    if "amount" in daily_df.columns and daily_df["amount"].notna().any():
        amount = daily_df["amount"]
    elif "volume" in daily_df.columns and daily_df["volume"].notna().any():
        amount = daily_df["volume"]
    else:
        amount = None

    idx = daily_df.index
    n = len(daily_df)
    result = pd.DataFrame(index=idx)

    # ── Trend 维度 ──

    # momentum_12_1
    ret_12m = close.pct_change(252) if n >= 252 else pd.Series(0.0, index=idx)
    ret_1m = close.pct_change(21) if n >= 21 else pd.Series(0.0, index=idx)
    result["momentum_12_1"] = ret_12m.fillna(0) - ret_1m.fillna(0)

    # trend_macd_5dim (周线 MACD 前向填充到日线)
    if weekly_df is not None and len(weekly_df) >= 5:
        w_dif, w_dea, w_hist = calc_macd(weekly_df["close"])
        w_close = weekly_df["close"]

        # 对每一周计算 trend 分数
        trend_scores = []
        for i in range(len(weekly_df)):
            if i < 5:
                trend_scores.append(15.0)
            else:
                try:
                    s = _score_trend_5dim(
                        w_dif.iloc[: i + 1],
                        w_dea.iloc[: i + 1],
                        w_hist.iloc[: i + 1],
                        w_close.iloc[: i + 1],
                    )
                    trend_scores.append(s)
                except Exception:
                    trend_scores.append(15.0)

        weekly_scores = pd.Series(trend_scores, index=weekly_df.index)
        result["trend_macd_5dim"] = (
            weekly_scores.reindex(idx, method="ffill").fillna(15.0)
        )
    else:
        result["trend_macd_5dim"] = 15.0

    # ── Alpha 维度 ──

    # reversal_20
    result["reversal_20"] = -close.pct_change(20).fillna(0)

    # oversold_60
    high_60 = high.rolling(60).max()
    result["oversold_60"] = (close / high_60 - 1).fillna(0)

    # weekly_deviation
    ma5 = close.rolling(5).mean()
    result["weekly_deviation"] = (close / ma5 - 1).fillna(0)

    # bottom_divergence (日线 MACD)
    _, _, daily_hist = calc_macd(close)
    result["bottom_divergence"] = _rolling_bottom_divergence(daily_df, daily_hist)

    # volume_reversal
    if amount is not None:
        result["volume_reversal"] = _rolling_volume_reversal(amount, close)
    else:
        result["volume_reversal"] = 0.5

    # bottom_fractal
    result["bottom_fractal"] = _rolling_bottom_fractal(daily_df)

    # ── Risk 维度 ──

    # low_volatility
    returns = close.pct_change()
    vol20 = returns.rolling(20).std()
    result["low_volatility"] = -vol20.fillna(0.03)

    # turnover_stability
    if amount is not None:
        vol_ma20 = amount.rolling(20).mean()
        vol_std20 = amount.rolling(20).std()
        cv = vol_std20 / vol_ma20.replace(0, np.nan)
        result["turnover_stability"] = -cv.fillna(0)
    else:
        result["turnover_stability"] = 0.0

    # close_position
    day_range = high - low
    result["close_position"] = (
        (close - low) / day_range.replace(0, np.nan)
    ).fillna(0.5)

    # ── 元数据 ──
    result["listing_days"] = np.arange(1, n + 1, dtype=int)
    result["is_st"] = False
    if amount is not None:
        result["daily_amount"] = amount.rolling(20).mean().fillna(0)
    else:
        result["daily_amount"] = 0.0

    return result


def precompute_stock_patterns(daily_df: pd.DataFrame) -> dict[str, pd.Series]:
    """预计算股票的形态检测序列（全历史一次性）。

    避免每月重复调用 detection functions (O(n²) → O(n))。

    返回: {
        "limit_up": pd.Series(bool),       # 涨停突破
        "platform": pd.Series(bool),       # 平台突破
        "ma_convergence": pd.Series(bool), # 均线粘合
        "double_bottom": pd.Series(bool),  # W底
        "flag_triangle": pd.Series(bool),  # 旗形/三角突破
    }
    """
    from factors.pattern import (
        detect_limit_up_breakout,
        detect_platform_breakout,
        detect_ma_convergence,
        detect_double_bottom,
        detect_flag_triangle_breakout,
    )

    return {
        "limit_up": detect_limit_up_breakout(daily_df),
        "platform": detect_platform_breakout(daily_df),
        "ma_convergence": detect_ma_convergence(daily_df),
        "double_bottom": detect_double_bottom(daily_df),
        "flag_triangle": detect_flag_triangle_breakout(daily_df),
    }


def lookup_pattern_multiplier(
    precomputed: dict[str, pd.Series],
    cut_end: pd.Timestamp,
    daily_df_slice: pd.DataFrame,
    weekly_df_slice: pd.DataFrame | None = None,
) -> float:
    """从预计算的形态数据快速计算形态乘数 (0.90-1.20)。

    参数:
        precomputed: precompute_stock_patterns() 的返回值
        cut_end: 截止日期（如 month_end），用于切片
        daily_df_slice: 日线切片（截至 cut_end，用于缠论）
        weekly_df_slice: 周线切片（用于缠论买点检测）
    """
    pattern_strength = 0.0

    # A. 涨停突破 (0-0.3) — 查表免计算
    lu = precomputed.get("limit_up")
    if lu is not None:
        lu_cut = lu.loc[:cut_end]
        if lu_cut.tail(20).any():
            pattern_strength += 0.30
        elif lu_cut.tail(60).any():
            pattern_strength += 0.15

    # B. 经典形态 (0-0.3) — 查表免计算
    classic_scores = {}
    for name, key in [
        ("平台突破", "platform"),
        ("均线粘合", "ma_convergence"),
        ("W底", "double_bottom"),
        ("旗形突破", "flag_triangle"),
    ]:
        s = precomputed.get(key)
        if s is None:
            continue
        cut = s.loc[:cut_end]
        if key == "platform":
            classic_scores[name] = min(1.0, cut.tail(20).sum() / 3)
        elif key == "ma_convergence":
            classic_scores[name] = min(1.0, cut.tail(20).sum() / 5)
        elif key == "double_bottom":
            classic_scores[name] = min(1.0, cut.tail(30).sum() / 2)
        elif key == "flag_triangle":
            classic_scores[name] = min(1.0, cut.tail(20).sum() / 2)

    if classic_scores:
        top2 = sorted(classic_scores.values(), reverse=True)[:2]
        pattern_strength += (sum(top2) / 2) * 0.30

    # C. 缠论买点 (0-0.4) — 保持原有计算（缠论速度已很快 ~0ms）
    if daily_df_slice is not None and weekly_df_slice is not None and len(weekly_df_slice) >= 5:
        try:
            from factors.macd import calc_macd
            w_dif, w_dea, w_hist = calc_macd(weekly_df_slice["close"])

            from factors.chanlun.zhongshu import detect_buy_points, identify_zhongshu
            zhongshus = identify_zhongshu(daily_df_slice)
            buy_points = detect_buy_points(
                daily_df_slice, w_dif, w_dea, w_hist, zhongshus,
            )
            if buy_points:
                best = max(bp.score for bp in buy_points)
                pattern_strength += min(0.40, best / 25 * 0.40)
        except Exception as e:
            logger.warning(f"缠论买点预计算失败: {e}")

    return round(0.90 + min(0.30, pattern_strength), 3)


def lookup_factors_at(
    precomputed: pd.DataFrame,
    date: pd.Timestamp,
    min_listing_days: int = 252,
    min_daily_amount: float = 1e5,
) -> dict | None:
    """从预计算结果中查找某日期的因子值。

    返回:
        dict (含 symbol + 各因子) 或 None (硬约束不通过)
    """
    if date not in precomputed.index:
        # 找最接近的 <= date 的日期
        available = precomputed.index[precomputed.index <= date]
        if len(available) == 0:
            return None
        date = available[-1]

    row = precomputed.loc[date]

    # 硬约束
    if row["listing_days"] < min_listing_days:
        return None
    if row["daily_amount"] < min_daily_amount:
        return None

    factors = {
        "trend_macd_5dim": float(row["trend_macd_5dim"]),
        "momentum_12_1": float(row["momentum_12_1"]),
        "reversal_20": float(row["reversal_20"]),
        "oversold_60": float(row["oversold_60"]),
        "weekly_deviation": float(row["weekly_deviation"]),
        "bottom_divergence": float(row["bottom_divergence"]),
        "volume_reversal": float(row["volume_reversal"]),
        "bottom_fractal": float(row["bottom_fractal"]),
        "low_volatility": float(row["low_volatility"]),
        "turnover_stability": float(row["turnover_stability"]),
        "close_position": float(row["close_position"]),
        "listing_days": int(row["listing_days"]),
        "daily_amount": float(row["daily_amount"]),
        "is_st": bool(row["is_st"]) if "is_st" in row else False,
    }

    return factors
