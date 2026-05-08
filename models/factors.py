"""因子计算模块 — 技术面/资金面/基本面因子"""

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

from core.logger import get_logger

logger = get_logger("models.factors")


# ===================== 技术指标工具函数 =====================

def calc_ma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def calc_macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = 2 * (dif - dea)
    return dif, dea, hist


def calc_amplitude(df: pd.DataFrame, window: int) -> pd.Series:
    """计算窗口期内价格振幅"""
    high = df["high"].rolling(window).max()
    low = df["low"].rolling(window).min()
    return high / low - 1


def calc_vol_ratio(volume: pd.Series, window: int) -> pd.Series:
    """当日成交量 vs 前N日均量比值"""
    return volume / volume.shift(1).rolling(window).mean()


def is_limit_up(close: pd.Series, open_: pd.Series, high: pd.Series, symbol: str) -> pd.Series:
    """判断涨停（区分主板/创业板/科创板）"""
    chg = close / open_ - 1
    if symbol.startswith("3") or symbol.startswith("688"):
        limit = 0.20
    elif symbol.startswith("30"):
        limit = 0.20
    else:
        limit = 0.10
    sealed = (close - high).abs() / close < 0.01
    return (chg >= limit * 0.98) & sealed


def is_breakout_candle(close: pd.Series, open_: pd.Series, threshold=0.03) -> pd.Series:
    """判断中阳线（3%+涨幅）"""
    return close / open_ - 1 >= threshold


# ===================== L1 趋势层 =====================

def calc_trend_layer(df: pd.DataFrame) -> pd.DataFrame:
    """
    L1 趋势过滤: MA20 > MA60 且价格在年线上方
    返回 DataFrame 含 trend_pass, ma20, ma60, ma250
    """
    close = df["close"]
    df = df.copy() if not isinstance(df, pd.DataFrame) else df
    df["ma20"] = calc_ma(close, 20)
    df["ma60"] = calc_ma(close, 60)
    df["ma250"] = calc_ma(close, 250)
    df["trend_pass"] = (df["ma20"] > df["ma60"]) & (close > df["ma250"])
    return df


# ===================== L2 结构层 =====================

def detect_platform(df: pd.DataFrame, min_days=60, max_amplitude=0.25):
    """
    检测平台整理区间。
    返回 (in_platform: bool, platform_high: float, platform_low: float)
    """
    if len(df) < min_days:
        return False, np.nan, np.nan

    recent = df.iloc[-min_days:]
    high = recent["high"].max()
    low = recent["low"].min()
    amp = high / low - 1
    return amp <= max_amplitude, high, low


def detect_platform_limit_up(df: pd.DataFrame, config: dict, symbol: str = "") -> pd.Series:
    """
    检测"平台内涨停"形态：
    - 过去N日在平台内
    - 当日涨停且close未突破平台上沿
    - 量能放大
    """
    result = pd.Series(0.0, index=df.index, dtype=float)
    if len(df) < config["min_days"] + 5:
        return result

    for i in range(config["min_days"], len(df)):
        window = df.iloc[i - config["min_days"] : i]
        today = df.iloc[i]
        close = today["close"]
        high_val = today["high"]

        # 平台判定
        platform_high = window["high"].max()
        platform_low = window["low"].min()
        amp = platform_high / platform_low - 1
        if amp > config["max_amplitude"]:
            continue

        # 涨停且封板，未突破平台上沿
        chg = close / today["open"] - 1
        prev_close = df["close"].iloc[i - 1] if i > 0 else today["open"]
        chg_pc = close / prev_close - 1
        is_cyb = symbol.startswith("3") or symbol.startswith("688")
        limit = 0.20 if is_cyb else 0.10
        sealed = abs(close - high_val) / close < 0.01
        is_lu = (chg >= limit * 0.90 or chg_pc >= limit * 0.98) and sealed and close < platform_high * 1.01

        if not is_lu:
            continue

        # 量能放大
        vol = today.get("volume", 0)
        vol_ma20 = df["volume"].iloc[max(0, i-20):i].mean()
        if vol_ma20 > 0 and vol / vol_ma20 >= config["vol_ratio"]:
            result.iloc[i] = 1.0

    return result


def detect_platform_breakout(df: pd.DataFrame, config: dict) -> pd.Series:
    """
    检测"涨停平台突破"：
    - 之前出现过平台内涨停（≥min_gap_days前）
    - 当前中阳线突破平台上沿
    - 量能配合
    """
    result = pd.Series(0.0, index=df.index, dtype=float)
    platform_highs = pd.Series(np.nan, index=df.index)

    # 先找所有平台区间（使用config参数）
    plat_days = config.get("min_days", 60)
    plat_amp = config.get("max_amplitude", 0.25)
    for i in range(plat_days, len(df)):
        window = df.iloc[i - plat_days : i]
        amp = window["high"].max() / window["low"].min() - 1
        if amp <= plat_amp:
            platform_highs.iloc[i] = window["high"].max()

    # 检测突破
    for i in range(config["min_gap_days"], len(df)):
        if pd.isna(platform_highs.iloc[i - 1]):
            continue

        today = df.iloc[i]
        ph = platform_highs.iloc[i - 1]
        if today["close"] > ph * 1.01:  # 突破平台上沿
            chg = today["close"] / today["open"] - 1
            if chg >= config["breakout_pct"]:
                vol = today.get("volume", 0)
                vol_ma20 = df["volume"].iloc[max(0, i-20):i].mean()
                if vol_ma20 > 0 and vol / vol_ma20 >= config["vol_ratio"]:
                    result.iloc[i] = 1.0

    return result


def detect_ma_converge(df: pd.DataFrame, config: dict) -> pd.Series:
    """
    检测均线粘合发散：
    - MA5/MA10/MA20/MA60 在N%范围内粘合 ≥ min_converge_days
    - 当前阳线向上发散
    """
    result = pd.Series(0.0, index=df.index, dtype=float)
    close = df["close"]
    mas = {w: calc_ma(close, w) for w in config["ma_list"]}
    min_days = config.get("min_converge_days", 15)

    converge_count = 0
    for i in range(60, len(df)):
        ma_vals = [mas[w].iloc[i] for w in config["ma_list"]]
        if any(pd.isna(v) for v in ma_vals):
            converge_count = 0
            continue

        ma_range = max(ma_vals) / min(ma_vals) - 1
        if ma_range <= config["converge_pct"]:
            converge_count += 1
        else:
            converge_count = 0
            continue

        # 粘合天数不足
        if converge_count < min_days:
            continue

        # 当前是阳线且短周期已上穿
        if df["close"].iloc[i] > df["open"].iloc[i]:
            if mas[5].iloc[i] > mas[10].iloc[i] > mas[20].iloc[i]:
                result.iloc[i] = 1.0

    return result


def detect_bottom_reversal(df: pd.DataFrame, config: dict) -> pd.Series:
    """
    检测底部反转（双底/W底简化版）：
    - 找到两个显著低点
    - 突破颈线确认
    """
    result = pd.Series(0.0, index=df.index, dtype=float)

    for i in range(config["min_formation_days"], len(df)):
        window = df.iloc[i - config["min_formation_days"] : i]
        low = window["low"]

        # 找局部低点
        local_min_idx = argrelextrema(low.values, np.less, order=5)[0]

        if len(local_min_idx) < 2:
            continue

        # 取最近两个低点
        idx1, idx2 = local_min_idx[-2], local_min_idx[-1]
        low1, low2 = low.iloc[idx1], low.iloc[idx2]

        if abs(low1 - low2) / low1 > config["double_bottom_tol"]:
            continue
        if idx2 - idx1 < config["double_bottom_gap"]:
            continue

        # 颈线 = 两低点之间的最高点
        neck = window["high"].iloc[idx1:idx2+1].max()

        # 当前突破颈线
        today = df.iloc[i]
        if today["close"] > neck * 1.01:
            result.iloc[i] = 1.0

    return result


# ===================== 结构层汇总 =====================

def calc_structure_scores(df: pd.DataFrame, config: dict, symbol: str = "") -> tuple:
    """计算 L2 结构层各形态信号，返回 (score, detail_dict)"""
    scores = {
        "platform_limit_up": detect_platform_limit_up(df, config["platform_limit_up"], symbol),
        "platform_breakout": detect_platform_breakout(df, config["platform_breakout"]),
        "ma_converge": detect_ma_converge(df, config["ma_converge"]),
        "bottom_reversal": detect_bottom_reversal(df, config["bottom_reversal"]),
        # 缠论中枢突破、旗形三角 需更复杂算法，暂用占位
        "zhongshu_breakout": pd.Series(0.0, index=df.index),
        "flag_triangle": pd.Series(0.0, index=df.index),
    }

    detail = {}
    total = pd.Series(0.0, index=df.index)
    for name, signal in scores.items():
        cfg = config.get(name, {})
        pts = cfg.get("score", 15)
        total += signal * pts
        detail[name] = signal

    return total, detail


# ===================== L3 时机层 =====================

def calc_timing_score(df: pd.DataFrame, config: dict) -> pd.Series:
    """L3 时机层: MACD金叉 + 量能放大"""
    close = df["close"]
    dif, dea, hist = calc_macd(
        close, config["macd_fast"], config["macd_slow"], config["macd_signal"]
    )

    macd_golden = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    vol_expand = df["volume"] / df["volume"].shift(1).rolling(5).mean()
    vol_ok = vol_expand > config["vol_expand_ratio"]

    score = pd.Series(0.0, index=df.index)
    score[macd_golden] += 0.5
    score[vol_ok & (dif > dea)] += 0.5
    return score


# ===================== L4 共振层 =====================

def calc_resonance_score(df_daily: pd.DataFrame, df_weekly: pd.DataFrame) -> pd.Series:
    """L4 共振层: 周线确认"""
    if df_weekly.empty:
        return pd.Series(0.0, index=df_daily.index)

    # 周线多头排列
    weekly_ma20 = calc_ma(df_weekly["close"], 20)
    weekly_ma60 = calc_ma(df_weekly["close"], 60)
    weekly_bull = weekly_ma20 > weekly_ma60

    # 映射回日线：每个日线日期取最近一周的多空判断
    score = pd.Series(0.0, index=df_daily.index)
    wb_sorted = weekly_bull.sort_index()
    for i, di in enumerate(df_daily.index):
        try:
            pos = wb_sorted.index.searchsorted(di)
            if pos > 0:
                score.iloc[i] = 1.0 if wb_sorted.iloc[pos - 1] else 0.0
            elif pos == 0 and wb_sorted.index[0] <= di:
                score.iloc[i] = 1.0 if wb_sorted.iloc[0] else 0.0
        except Exception as e:
            logger.warning(f"周线看涨信号对齐失败: {e}")
    return score


# ===================== 资金面因子 =====================

def calc_money_flow_factor(
    close: pd.Series, volume: pd.Series,
    high: pd.Series, low: pd.Series, window=10
) -> pd.Series:
    """简化版资金流因子: 价量关系"""
    typical = (high + low + close) / 3
    raw_mf = typical * volume
    pos = raw_mf.where(typical > typical.shift(1), 0).rolling(window).sum()
    neg = raw_mf.where(typical < typical.shift(1), 0).rolling(window).sum()
    mfi = 100 - 100 / (1 + pos / neg.replace(0, np.nan))
    return mfi


def calc_turnover_factor(turnover: pd.Series, window=10) -> pd.Series:
    """换手率异常因子: 低换手=锁仓"""
    avg = turnover.rolling(window).mean()
    return -turnover / avg  # 低换手=高分


# ===================== 基本面因子 =====================

def calc_accruals_factor(close: pd.Series, volume: pd.Series, window=60) -> pd.Series:
    """简化应计因子: 价格动量和成交量的背离"""
    p_chg = close.pct_change(window)
    v_chg = volume.pct_change(window)
    return p_chg - v_chg  # 价涨量跌=低应计=高分


def calc_idiosyncratic_vol(close: pd.Series, market_ret: pd.Series, window=60) -> pd.Series:
    """特质波动率: 残差标准差（需要市场收益序列）"""
    ret = close.pct_change()
    if len(market_ret) != len(ret):
        return pd.Series(np.nan, index=close.index)

    # 滚动回归残差
    resid_std = pd.Series(np.nan, index=close.index)
    for i in range(window, len(ret)):
        try:
            y = ret.iloc[i-window:i].values
            x = market_ret.iloc[i-window:i].values
            mask = ~(np.isnan(y) | np.isnan(x))
            if mask.sum() < 20:
                continue
            beta = np.polyfit(x[mask], y[mask], 1)[0]
            resid = y - beta * x
            resid_std.iloc[i] = np.nanstd(resid)
        except Exception as e:
            logger.warning(f"特质波动率回归失败: {e}")
    return -resid_std  # 低特质波动=高分


def calc_short_reversal(close: pd.Series, window=5) -> pd.Series:
    """短期反转因子: 最近跌=高分(逆向)"""
    return -close.pct_change(window)


# ===================== 综合因子 =====================

def safe_expanding_normalize(s: pd.Series) -> pd.Series:
    """expanding max 归一化：每个时间点只用过去数据的最大值，杜绝前视偏差"""
    emax = s.expanding().max().replace(0, np.nan)
    result = (s / emax * 100)
    return result.fillna(0)


def calc_all_factors(
    df_daily: pd.DataFrame,
    df_weekly: pd.DataFrame = None,
    market_ret: pd.Series = None,
    symbol: str = "",
) -> pd.DataFrame:
    """计算所有因子，返回 DataFrame"""
    from config.settings import (
        STRUCTURE_CONFIG, TIMING_CONFIG, CAPITAL_CONFIG, FUNDAMENTAL_CONFIG, PIPE_WEIGHTS,
    )

    df = df_daily.copy()
    close, high, low, open_, volume = df["close"], df["high"], df["low"], df["open"], df["volume"]
    turnover = df.get("turnover", pd.Series(np.nan, index=df.index))

    # L1 趋势
    df = calc_trend_layer(df)

    # L2 结构
    struct_score, struct_detail = calc_structure_scores(df, STRUCTURE_CONFIG, symbol)

    # L3 时机
    timing_score = calc_timing_score(df, TIMING_CONFIG)

    # L4 共振
    if df_weekly is not None and not df_weekly.empty:
        resonance = calc_resonance_score(df, df_weekly)
    else:
        resonance = pd.Series(0.0, index=df.index)

    # 技术面总分
    tech_score = (
        df["trend_pass"].astype(float) * 20 +
        struct_score * 0.5 +
        timing_score * 10 +
        resonance * 10
    )
    tech_score = safe_expanding_normalize(tech_score)

    # 资金面
    money_flow = calc_money_flow_factor(close, volume, high, low)
    tn_factor = calc_turnover_factor(turnover) if not turnover.isna().all() else pd.Series(0.0, index=df.index)
    cap_score = money_flow.fillna(0) * 0.4 + tn_factor.fillna(0) * 0.3
    cap_score = safe_expanding_normalize(cap_score)

    # 基本面
    accruals = calc_accruals_factor(close, volume)
    ivol = calc_idiosyncratic_vol(close, market_ret) if market_ret is not None else pd.Series(0.0, index=df.index)
    reversal = calc_short_reversal(close)
    fund_score = accruals.fillna(0) * 0.3 + ivol.fillna(0) * 0.2 + reversal.fillna(0) * 0.3
    fund_score = safe_expanding_normalize(fund_score)

    # 综合
    df["score_technical"] = tech_score
    df["score_capital"] = cap_score
    df["score_fundamental"] = fund_score
    df["score_total"] = (
        tech_score * PIPE_WEIGHTS["technical"] +
        cap_score * PIPE_WEIGHTS["capital"] +
        fund_score * PIPE_WEIGHTS["fundamental"]
    )

    return df
