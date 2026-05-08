"""资金面数据获取 — 北向/大单/融资，含降级"""

import pandas as pd

from data.sources import get_fundflow_source
from shared.cache import read_cache, write_cache
from core.logger import get_logger

logger = get_logger("data.fund_flow")


def fetch_northbound_flow(start: str, end: str) -> pd.DataFrame | None:
    """获取北向资金净流入数据"""
    cache_key = f"northbound_{start}_{end}"
    cached = read_cache(cache_key, ttl_seconds=1800)
    if cached is not None and not cached.empty:
        return cached

    source = get_fundflow_source()
    try:
        df = source.fetch_northbound_flow(start, end)
        if df is not None and not df.empty:
            write_cache(df, cache_key)
        return df
    except Exception as e:
        logger.warning(f"北向资金获取失败: {e}")
        return None


def calc_northbound_inflow_strength(northbound_df: pd.DataFrame,
                                    window: int = 20) -> float:
    """计算北向流入强度（0-100）"""
    if northbound_df is None or northbound_df.empty:
        return 50.0

    if "net_flow" not in northbound_df.columns:
        return 50.0

    recent = northbound_df["net_flow"].tail(window)
    total_inflow = float(recent.sum())
    avg_daily = float(recent.mean())

    # 归一化：日均流入/日均成交额均值 映射到 0-100
    strength = min(100, max(0, avg_daily / 1e8 * 50 + 50))
    return strength


def is_northbound_inflow_positive(northbound_df: pd.DataFrame | None,
                                  window: int = 20) -> bool:
    """北向资金周期累计是否净流入"""
    if northbound_df is None or northbound_df.empty:
        return False
    if "net_flow" not in northbound_df.columns:
        return False
    return float(northbound_df["net_flow"].tail(window).sum()) > 0


def check_consecutive_outflow(northbound_df: pd.DataFrame | None,
                              days: int = 3) -> bool:
    """检查是否连续N日净流出（退出信号）"""
    if northbound_df is None or northbound_df.empty:
        return False
    if "net_flow" not in northbound_df.columns:
        return False
    recent = northbound_df["net_flow"].tail(days)
    return bool((recent < 0).all())


def get_fund_data_for_stock(symbol: str, start: str, end: str) -> dict:
    """
    获取单只股票的资金面数据（含降级标记）。

    返回: {northbound_available, northbound_net_flow, ...}
    """
    nb = fetch_northbound_flow(start, end)
    fund_data = {
        "northbound_available": nb is not None and not nb.empty,
        "northbound_net_flow": float(nb["net_flow"].tail(10).sum()) if nb is not None and not nb.empty else 0,
        "big_order_available": False,  # 后续实现
        "margin_available": False,     # 后续实现
        "turnover_available": False,   # 后续实现
        "turnover_trend_up": False,
    }
    return fund_data
