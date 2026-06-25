"""一站式下载全部信号数据"""

import time
from datetime import datetime

from sniper.signals.client import safe_request
from sniper.signals.store import SignalStore
from sniper.signals.download_northbound import download_northbound
from sniper.signals.download_fund_flow import download_fund_flow
from sniper.signals.download_dragon_tiger import download_dragon_tiger
from sniper.signals.download_industry import download_industry_compare
from sniper.signals.download_hot_stocks import download_hot_stocks
from sniper.signals.download_financials import download_quarterly
from sniper.signals.compute_from_bars import compute_fund_flow_proxy, compute_hot_stocks
from core.logger import get_logger

logger = get_logger("sniper.signals.download_all")


def download_all_signals(
    store: SignalStore,
    start: str = "2019-01-01",
    end: str | None = None,
    batch_size: int = 50,
    delay: float = 0.3,
) -> dict[str, int]:
    """一站式下载全部信号数据，返回每张表的写入行数。"""
    end = end or datetime.now().strftime("%Y-%m-%d")
    results: dict[str, int] = {}

    # 按依赖顺序执行：北向资金（独立）→ 行业对比（独立）→ 强势股（独立）→ 龙虎榜（独立）→ 资金流向（逐股）→ 财务数据（逐股）
    pipeline = [
        ("北向资金", download_northbound, {"start": start, "end": end}),
        ("行业对比", download_industry_compare, {"start": start, "end": end}),
        ("强势股", download_hot_stocks, {"start": start, "end": end}),
        ("龙虎榜", download_dragon_tiger, {"start": start, "end": end}),
        ("资金流向", download_fund_flow, {"start": start, "end": end, "batch_size": batch_size, "delay": delay}),
        ("财务数据", download_quarterly, {"batch_size": batch_size, "delay": delay}),
    ]

    for name, func, kwargs in pipeline:
        logger.info(f"开始下载 {name}...")
        t0 = time.time()
        try:
            rows = func(store, **kwargs)
            elapsed = time.time() - t0
            results[name] = rows
            logger.info(f"{name} 完成: {rows} 行, 耗时 {elapsed:.1f}s")
        except Exception as e:
            logger.error(f"{name} 下载失败: {e}")
            results[name] = -1

    return results
