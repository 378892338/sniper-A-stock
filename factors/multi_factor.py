"""多因子评分模块 — Phase 1 纯量价版

三维度评分体系（总分100）:
  Trend(30):  5维MACD(15) + momentum_12_1(15)         — 趋势确认
  Alpha(40):  价格反转(22) + 技术确认(18)               — 买点信号
  Risk(30):   low_volatility(15) + turnover_stability(10) + close_position(5)

设计原则:
  - 纯量价因子，不依赖基本面数据（Phase 2 再加入 EP/BP）
  - 因子计算是"只看自己"的个股独立计算
  - 截面标准化由 process_cross_section() 统一处理
  - 大盘过滤由 check_market_trend() 提供权重系数
"""

import numpy as np
import pandas as pd

from factors.macd import calc_macd
from factors.volume import score_volume_three_part

# ═══════════════════════════════════════════════════════════════════════
# 权重配置
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_WEIGHTS = {
    "trend_macd_5dim": 15,
    "momentum_12_1": 15,
    "reversal_20": 12,
    "oversold_60": 6,
    "weekly_deviation": 4,
    "bottom_divergence": 8,
    "volume_reversal": 6,
    "bottom_fractal": 4,
    "low_volatility": 15,
    "turnover_stability": 10,
    "close_position": 5,
}

# 大盘60日均线向下时，Alpha维度的降权系数
BEAR_MARKET_ALPHA_MULTIPLIER = 0.3

# 硬约束
MIN_FLOAT_MCAP = 30e8       # 30亿流通市值
MIN_DAILY_AMOUNT = 1e5       # 日成交额门槛（缓存数据中 amount 单位因数据源而异，设保守值）
MIN_LISTING_DAYS = 252      # 上市满1年


# ═══════════════════════════════════════════════════════════════════════
# 硬约束
# ═══════════════════════════════════════════════════════════════════════

def apply_hard_filters(df: pd.DataFrame) -> pd.DataFrame:
    """硬约束剔除：ST、流动性、市值、上市时间"""
    mask = pd.Series(True, index=df.index)

    if "is_st" in df.columns:
        mask &= ~df["is_st"]
    if "float_mcap" in df.columns:
        mask &= df["float_mcap"] >= MIN_FLOAT_MCAP
    if "daily_amount" in df.columns:
        mask &= df["daily_amount"] >= MIN_DAILY_AMOUNT
    if "listing_days" in df.columns:
        mask &= df["listing_days"] >= MIN_LISTING_DAYS

    return df[mask]


# ═══════════════════════════════════════════════════════════════════════
# 原始因子计算（个股独立，无截面信息）
# ═══════════════════════════════════════════════════════════════════════

def compute_raw_factors(
    symbol: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame | None = None,
    monthly_df: pd.DataFrame | None = None,
) -> dict | None:
    """计算单只股票的原始因子值（未标准化）。

    返回 None 表示该股票在本月不纳入评分池。
    """
    close = daily_df["close"]
    high = daily_df["high"]
    low = daily_df["low"]

    # 成交量/成交额：优先用 amount，没有则用 volume
    if "amount" in daily_df.columns and daily_df["amount"].notna().any():
        volume_or_amount = daily_df["amount"]
    elif "volume" in daily_df.columns and daily_df["volume"].notna().any():
        volume_or_amount = daily_df["volume"]
    else:
        volume_or_amount = None

    n = len(close)
    if n < 60:
        return None

    # 上市时间不足1年 → 剔除（检查点2）
    if n < MIN_LISTING_DAYS:
        return None

    factors = {"symbol": symbol}

    # ── Trend 维度 ──

    # 5维MACD (标量0-30)
    try:
        weekly_dif, weekly_dea, weekly_hist = calc_macd(weekly_df["close"]) if weekly_df is not None and not weekly_df.empty else (None, None, None)
        from gate.layer2_sector import _score_trend_5dim
        factors["trend_macd_5dim"] = _score_trend_5dim(weekly_dif, weekly_dea, weekly_hist, weekly_df["close"]) if weekly_dif is not None else 15.0
    except Exception:
        factors["trend_macd_5dim"] = 15.0

    # momentum_12_1 (12月收益 - 1月收益)
    if n >= 252:
        ret_12m = float(close.iloc[-1] / close.iloc[-252] - 1)
        ret_1m = float(close.iloc[-1] / close.iloc[-21] - 1) if n >= 21 else 0.0
        factors["momentum_12_1"] = ret_12m - ret_1m
    else:
        factors["momentum_12_1"] = 0.0

    # ── Alpha: 价格反转 ──

    # reversal_20 (负20日收益 = 反转潜力)
    ret_20 = float(close.iloc[-1] / close.iloc[-21] - 1) if n >= 21 else 0.0
    factors["reversal_20"] = -ret_20

    # oversold_60 (距60日最高点的回撤幅度)
    high_60 = float(high.tail(60).max())
    factors["oversold_60"] = (float(close.iloc[-1]) / high_60 - 1) if high_60 > 0 else 0.0

    # weekly_deviation (周线偏离度)
    if len(close) >= 5:
        ma5 = float(close.rolling(5).mean().iloc[-1])
        factors["weekly_deviation"] = (float(close.iloc[-1]) / ma5 - 1) if ma5 > 0 else 0.0
    else:
        factors["weekly_deviation"] = 0.0

    # ── Alpha: 技术确认 ──

    # 底背驰 (日线)
    factors["bottom_divergence"] = _score_bottom_divergence(daily_df)

    # 量能反转
    factors["volume_reversal"] = _score_volume_reversal(volume_or_amount, close) if volume_or_amount is not None else 0.5

    # 底分型
    factors["bottom_fractal"] = _score_bottom_fractal(daily_df)

    # ── Risk 维度 ──

    # low_volatility (20日波动率倒数)
    returns = close.pct_change().dropna().tail(20)
    vol20 = float(returns.std()) if len(returns) >= 10 else 0.03
    factors["low_volatility"] = -vol20

    # turnover_stability (换手率变异系数倒数)
    if volume_or_amount is not None and len(volume_or_amount) >= 20:
        vol_recent = volume_or_amount.tail(20)
        vol_mean = float(vol_recent.mean())
        vol_std = float(vol_recent.std())
        cv = vol_std / vol_mean if vol_mean > 0 else 1.0
        factors["turnover_stability"] = -cv
    else:
        factors["turnover_stability"] = 0.0

    # close_position (日内收盘位置)
    if high.iloc[-1] > low.iloc[-1]:
        factors["close_position"] = float(close.iloc[-1] - low.iloc[-1]) / float(high.iloc[-1] - low.iloc[-1])
    else:
        factors["close_position"] = 0.5

    # ── 硬约束标记 ──
    factors["listing_days"] = n
    factors["daily_amount"] = float(volume_or_amount.tail(20).mean()) if volume_or_amount is not None else 0.0
    factors["is_st"] = False

    return factors


# ═══════════════════════════════════════════════════════════════════════
# 技术确认子因子
# ═══════════════════════════════════════════════════════════════════════

def _score_bottom_divergence(daily_df: pd.DataFrame) -> float:
    """底背驰得分 (0-1)。

    使用日线底背驰检测，近5日出现底背驰信号 → 1.0
    """
    try:
        from factors.chanlun.divergence import check_daily_bottom_divergence
        _, _, daily_hist = calc_macd(daily_df["close"])
        has_div = check_daily_bottom_divergence(daily_df, daily_hist)
        return 1.0 if has_div else 0.0
    except Exception:
        return 0.0


def _score_volume_reversal(volume: pd.Series, close: pd.Series, period: int = 10) -> float:
    """量能反转得分 (0-1)。

    下跌缩量 + 反弹放量 = 量能反转确认。
    比值 = 下跌日平均量 / 上涨日平均量，< 0.8 得高分。
    """
    if len(volume) < period:
        return 0.5

    recent_vol = volume.tail(period)
    recent_close = close.tail(period)
    pct_chg = recent_close.pct_change()

    up_mask = pct_chg > 0.002
    down_mask = pct_chg < -0.002

    up_vol = float(recent_vol[up_mask.values].mean()) if up_mask.any() else None
    down_vol = float(recent_vol[down_mask.values].mean()) if down_mask.any() else None

    if up_vol is None or down_vol is None or down_vol == 0:
        return 0.5

    ratio = down_vol / up_vol
    return round(max(0.0, min(1.0, (1.5 - ratio) / 0.9)), 3)


def _score_bottom_fractal(daily_df: pd.DataFrame, window: int = 10) -> float:
    """底分型得分 (0-1)。

    严格底分型:
      1. 标准三K线形态: 中间K线低点 < 左、右K线低点
      2. 前序下跌: 前5根K线低点严格逐步下移
      3. 后续确认: 下一根K线收盘价 > 分型K线最高价（强势反转确认）
    """
    if len(daily_df) < 10:
        return 0.0

    recent = daily_df.tail(window + 6)
    low = recent["low"].values
    high = recent["high"].values
    close = recent["close"].values
    n = len(recent)

    for i in range(6, n - 2):
        if not (low[i] < low[i - 1] and low[i] < low[i + 1]):
            continue

        # 前5根K线低点严格逐步下移
        prior = True
        for j in range(1, 5):
            if low[i - j] >= low[i - j - 1]:
                prior = False
                break
        if not prior:
            continue

        # 确认K线收盘价必须突破分型K线最高价
        if close[i + 1] <= high[i]:
            continue

        return 1.0

    return 0.0


# ═══════════════════════════════════════════════════════════════════════
# 截面标准化
# ═══════════════════════════════════════════════════════════════════════

_FACTOR_COLUMNS = [
    "trend_macd_5dim", "momentum_12_1",
    "reversal_20", "oversold_60", "weekly_deviation",
    "bottom_divergence", "volume_reversal", "bottom_fractal",
    "low_volatility", "turnover_stability", "close_position",
]


def mad_winsorize(series: pd.Series, k: float = 5.0) -> pd.Series:
    """MAD 去极值：将超出 median ± k*MAD 的值截断到边界"""
    med = series.median()
    mad = (series - med).abs().median()
    if mad == 0:
        return series
    upper = med + k * mad
    lower = med - k * mad
    return series.clip(lower, upper)


def standardize(series: pd.Series) -> pd.Series:
    """Z-Score 标准化"""
    std = series.std()
    if std == 0:
        return pd.Series(0.0, index=series.index)
    return (series - series.mean()) / std


def process_cross_section(factors_list: list[dict]) -> pd.DataFrame:
    """对一批股票的原始因子做截面处理：去极值 + 标准化 + NaN填充。

    factors_list: [{"symbol": "...", "reversal_20": 0.05, ...}, ...]
    返回: DataFrame，索引为 symbol，列为标准化后的因子值
    """
    df = pd.DataFrame(factors_list)
    df = df.set_index("symbol")

    # 只处理因子列
    factor_cols = [c for c in _FACTOR_COLUMNS if c in df.columns]
    if not factor_cols:
        return pd.DataFrame()

    for col in factor_cols:
        series = df[col].dropna()
        if len(series) < 5:
            df[col] = 0.0
            continue
        # 扩展回原索引做去极值和标准化
        aligned = df[col].copy()
        med = series.median()
        mad = (series - med).abs().median()
        if mad > 0:
            upper = med + 5.0 * mad
            lower = med - 5.0 * mad
            aligned = aligned.clip(lower, upper)
        std = series.std()
        if std > 0:
            aligned = (aligned - series.mean()) / std
        else:
            aligned = pd.Series(0.0, index=df.index)
        # 检查点5: NaN 用截面中位数填充（标准化后即0）
        aligned = aligned.fillna(0.0)
        df[col] = aligned

    # 保留元数据列
    for meta_col in ["listing_days", "daily_amount", "is_st", "float_mcap"]:
        if meta_col in df.columns:
            pass  # 保留不动

    return df


# ═══════════════════════════════════════════════════════════════════════
# 评分合成
# ═══════════════════════════════════════════════════════════════════════

def get_dynamic_weights(market_state: str | None = None) -> dict[str, float]:
    """根据市场状态返回动态因子权重。

    牛市: momentum↑ reversal↓ (动量主导)
    震荡: 默认权重
    偏弱: reversal↑ momentum↓ (反转主导)
    熊市: reversal主导 但整体通过 alpha_multiplier 降权
    """
    w = DEFAULT_WEIGHTS.copy()

    if market_state is None:
        return w

    if market_state in ("bull", "牛市"):
        # 动量增强: momentum_12_1 +5, reversal_20 -4
        w["momentum_12_1"] = min(20, w["momentum_12_1"] + 5)
        w["reversal_20"] = max(6, w["reversal_20"] - 6)
        w["oversold_60"] = max(2, w["oversold_60"] - 4)
    elif market_state in ("weak", "偏弱"):
        # 反转增强: reversal_20 +5, momentum_12_1 -5
        w["reversal_20"] = min(18, w["reversal_20"] + 6)
        w["oversold_60"] = min(10, w["oversold_60"] + 4)
        w["momentum_12_1"] = max(8, w["momentum_12_1"] - 7)
    elif market_state in ("bear", "熊市"):
        # 反转主导 + 整体降权（由 alpha_multiplier 处理）
        w["reversal_20"] = min(18, w["reversal_20"] + 6)
        w["momentum_12_1"] = max(5, w["momentum_12_1"] - 10)

    return w


def aggregate_scores(
    factors_df: pd.DataFrame,
    weights: dict[str, float] | None = None,
    alpha_multiplier: float = 1.0,
    market_state: str | None = None,
) -> pd.DataFrame:
    """将标准化后的因子加权合成为最终评分。

    factors_df: process_cross_section 的输出
    weights: 因子权重字典，默认使用 DEFAULT_WEIGHTS（根据 market_state 动态调整）
    alpha_multiplier: Alpha维度的乘数（大盘弱市时降权）
    market_state: L1 市场状态 (bull/volatile/weak/bear 或 牛市/震荡/偏弱/熊市)

    返回: DataFrame，包含 score, trend_score, alpha_score, risk_score 列
    """
    if weights is None:
        weights = get_dynamic_weights(market_state)

    result = pd.DataFrame(index=factors_df.index)
    result["trend_score"] = 0.0
    result["alpha_score"] = 0.0
    result["risk_score"] = 0.0

    # Trend 因子
    trend_factors = ["trend_macd_5dim", "momentum_12_1"]
    for f in trend_factors:
        if f in factors_df.columns:
            result["trend_score"] += factors_df[f] * weights.get(f, 0)

    # Alpha 因子
    alpha_factors = [
        "reversal_20", "oversold_60", "weekly_deviation",
        "bottom_divergence", "volume_reversal", "bottom_fractal",
    ]
    for f in alpha_factors:
        if f in factors_df.columns:
            result["alpha_score"] += factors_df[f] * weights.get(f, 0)
    result["alpha_score"] *= alpha_multiplier

    # Risk 因子
    risk_factors = ["low_volatility", "turnover_stability", "close_position"]
    for f in risk_factors:
        if f in factors_df.columns:
            result["risk_score"] += factors_df[f] * weights.get(f, 0)

    result["score"] = (
        result["trend_score"] + result["alpha_score"] + result["risk_score"]
    )

    # 将子维度缩放到 0-100 便于比较
    result["score"] = _rescale_to_100(result["score"])

    return result


def _rescale_to_100(series: pd.Series) -> pd.Series:
    """将任意分布的得分线性映射到 0-100"""
    mn = series.min()
    mx = series.max()
    if mx - mn < 1e-10:
        return pd.Series(50.0, index=series.index)
    return ((series - mn) / (mx - mn) * 100).round(1)


# ═══════════════════════════════════════════════════════════════════════
# 大盘趋势过滤（检查点4: t-1 时点）
# ═══════════════════════════════════════════════════════════════════════

def check_market_trend(
    hs300_daily: pd.DataFrame,
    current_date: pd.Timestamp,
) -> float:
    """检查大盘60日均线方向，返回 Alpha 维度乘数。

    current_date: 调仓决策时点（如周五）
    使用 current_date 前一天及之前的数据计算（检查点4: t-1）

    返回: 1.0（牛市）或 BEAR_MARKET_ALPHA_MULTIPLIER（熊市/震荡）
    """
    # 严格使用 t-1 及之前的数据
    cutoff = current_date - pd.Timedelta(days=1)
    hist = hs300_daily.loc[:cutoff]

    if len(hist) < 60:
        return 1.0  # 数据不足，不降权

    close = hist["close"]
    ma60 = float(close.rolling(60).mean().iloc[-1])
    current = float(close.iloc[-1])

    if current > ma60:
        return 1.0
    return BEAR_MARKET_ALPHA_MULTIPLIER


# ═══════════════════════════════════════════════════════════════════════
# 便捷入口：单月批量评分
# ═══════════════════════════════════════════════════════════════════════

def score_monthly_batch(
    symbols: list[str],
    month_end: pd.Timestamp,
    store,  # DataStore
    hs300_daily: pd.DataFrame | None = None,
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    """对一个月末的所有股票做批量评分（含截面标准化）。

    返回: DataFrame，列为 symbol, month, score, trend_score, alpha_score, risk_score
    """
    # Step 1: 逐个计算原始因子
    all_factors = []
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is None or len(daily) < 60:
            continue
        daily_cut = daily.loc[:month_end]
        if len(daily_cut) < 60:
            continue

        weekly = store.get_weekly(sym)
        monthly = store.get_monthly(sym)
        weekly_cut = weekly.loc[:month_end] if weekly is not None else None
        monthly_cut = monthly.loc[:month_end] if monthly is not None else None

        f = compute_raw_factors(sym, daily_cut, weekly_cut, monthly_cut)
        if f is not None:
            all_factors.append(f)

    if not all_factors:
        return pd.DataFrame()

    # Step 2: 硬约束
    factors_df = pd.DataFrame(all_factors)
    factors_df = apply_hard_filters(factors_df)
    if factors_df.empty:
        return pd.DataFrame()

    # Step 3: 截面标准化
    symbols_list = factors_df["symbol"].tolist()
    processed = process_cross_section(factors_df.to_dict("records"))

    # Step 4: 大盘过滤
    alpha_mult = 1.0
    if hs300_daily is not None:
        alpha_mult = check_market_trend(hs300_daily, month_end)

    # Step 5: 评分合成
    scores = aggregate_scores(processed, weights=weights, alpha_multiplier=alpha_mult)

    scores["symbol"] = scores.index
    scores["month"] = month_end.strftime("%Y-%m")

    return scores[["symbol", "month", "score", "trend_score", "alpha_score", "risk_score"]]
