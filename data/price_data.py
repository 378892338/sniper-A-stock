"""个股行情数据获取 — 日/周/月线"""

import time
import pandas as pd

from data.sources import get_source
from shared.cache import read_cache, write_cache
from core.logger import get_logger

logger = get_logger("data.price")


def fetch_daily(symbol: str, start: str, end: str,
                source: str = "akshare") -> pd.DataFrame:
    """获取个股日线"""
    cache_key = f"daily_{symbol}_{start}_{end}"
    cached = read_cache(cache_key, ttl_seconds=1800)
    if cached is not None and not cached.empty:
        return cached

    ds = get_source()
    df = ds.fetch_daily(symbol, start, end)
    if not df.empty:
        write_cache(df, cache_key)
    return df


def fetch_weekly(symbol: str, start: str, end: str) -> pd.DataFrame:
    """获取个股周线（日线resample）"""
    daily = fetch_daily(symbol, start, end)
    if daily.empty:
        return daily

    weekly = daily.resample("W-FRI", closed="right", label="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    })
    return weekly.dropna()


def fetch_monthly(symbol: str, start: str, end: str) -> pd.DataFrame:
    """获取个股月线（日线resample）"""
    daily = fetch_daily(symbol, start, end)
    if daily.empty:
        return daily

    monthly = daily.resample("ME", closed="right", label="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    })
    return monthly.dropna()


def fetch_batch_daily(symbols: list[str], start: str, end: str,
                      sleep: float = 0.3) -> dict[str, pd.DataFrame]:
    """批量获取日线"""
    result = {}
    for i, sym in enumerate(symbols):
        df = fetch_daily(sym, start, end)
        if not df.empty:
            result[sym] = df
        if (i + 1) % 20 == 0:
            logger.info(f"个股行情: {i+1}/{len(symbols)}")
        time.sleep(sleep)
    return result
