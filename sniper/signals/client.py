"""a-stock-data 信号下载 — 统一客户端

所有下载函数从 a-stock-data 支持的底层源获取数据，写入 SignalStore。
回测和实盘不依赖这些 API，仅在数据初始化/更新时调用。
"""

import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from core.logger import get_logger

logger = get_logger("sniper.signals.client")


def safe_request(fn, *args, max_retries: int = 3, delay: float = 1.0, **kwargs) -> Any:
    """带重试和安全处理的请求包装。"""
    for attempt in range(max_retries):
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as e:
            if attempt < max_retries - 1:
                wait = delay * (attempt + 1)
                logger.warning(f"重试 {attempt + 1}/{max_retries}: {e}, 等待 {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"请求失败已达最大重试次数: {e}")
                return None


def get_trading_dates(start: str = "2019-01-01", end: str | None = None) -> list[str]:
    """获取交易日列表（用 akshare 交易日历）。"""
    import akshare as ak
    try:
        df = safe_request(ak.tool_trade_date_hist_sina)
        if df is not None and not df.empty:
            mask = (df["trade_date"] >= start) & (df["trade_date"] <= (end or datetime.now().strftime("%Y-%m-%d")))
            return sorted(df[mask]["trade_date"].dt.strftime("%Y-%m-%d").tolist())
    except Exception as e:
        logger.warning(f"获取交易日历失败: {e}")
    # 兜底：用 pandas 生成
    dates = pd.bdate_range(start, end or datetime.now().strftime("%Y-%m-%d"))
    return [d.strftime("%Y-%m-%d") for d in dates]
