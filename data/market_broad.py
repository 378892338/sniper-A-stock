"""市场宽基指数数据 — 上证/深证/创业板 + 沪深300(Alpha基准)"""

import pandas as pd

from data.sources import get_source
from shared.cache import read_cache, write_cache
from shared.retry import retry
from core.logger import get_logger

logger = get_logger("data.market_broad")

# 三大市场指数（第一层评估对象）
MARKET_INDEXES = {
    "shanghai": "000001",   # 上证指数
    "shenzhen": "399001",   # 深证成指
    "chinext": "399006",    # 创业板指
}

# 沪深300 — 第二层 Alpha 统一基准
CSI300_CODE = "000300"


def fetch_market_index(index_code: str, start: str, end: str,
                       source: str = "akshare") -> pd.DataFrame:
    """获取大盘指数日线"""
    cache_key = f"market_index_{index_code}_{start}_{end}"
    cached = read_cache(cache_key, ttl_seconds=3600)
    if cached is not None and not cached.empty:
        return cached

    ds = get_source()
    try:
        df = ds.fetch_index_daily(index_code, start, end)
        if not df.empty:
            write_cache(df, cache_key)
        return df
    except Exception as e:
        logger.error(f"获取指数 {index_code} 失败: {e}")
        return pd.DataFrame()


def get_all_market_data(start: str, end: str) -> dict[str, pd.DataFrame]:
    """获取三大市场的日线数据"""
    result = {}
    for name, code in MARKET_INDEXES.items():
        df = fetch_market_index(code, start, end)
        if not df.empty:
            result[name] = df
            logger.info(f"{name} ({code}): {len(df)} 条日线")
        else:
            logger.warning(f"{name} ({code}): 数据获取失败")
    return result


def resample_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """日线转周线"""
    if daily_df.empty:
        return daily_df
    weekly = daily_df.resample("W-FRI", closed="right", label="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    })
    return weekly.dropna()


def resample_to_monthly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """日线转月线"""
    if daily_df.empty:
        return daily_df
    monthly = daily_df.resample("ME", closed="right", label="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    })
    return monthly.dropna()


def fetch_csi300(start: str, end: str, source: str = "akshare") -> pd.DataFrame:
    """获取沪深300指数日线（Alpha基准）"""
    cache_key = f"csi300_{start}_{end}"
    cached = read_cache(cache_key, ttl_seconds=3600)
    if cached is not None and not cached.empty:
        return cached

    ds = get_source()
    try:
        df = ds.fetch_index_daily(CSI300_CODE, start, end)
        if not df.empty:
            write_cache(df, cache_key)
            logger.info(f"沪深300: {len(df)} 条日线")
        return df
    except Exception as e:
        logger.error(f"获取沪深300失败: {e}")
        return pd.DataFrame()


def get_csi300_weekly(start: str, end: str) -> pd.Series | None:
    """获取沪深300周线close序列（供Alpha计算）"""
    daily = fetch_csi300(start, end)
    if daily.empty:
        return None
    weekly = resample_to_weekly(daily)
    return weekly["close"]
