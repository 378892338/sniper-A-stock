"""下载强势股+题材归因 — akshare 东方财富涨停池"""

import time
import akshare as ak
import pandas as pd

from sniper.signals.client import safe_request
from sniper.signals.store import SignalStore
from core.logger import get_logger

logger = get_logger("sniper.signals.download_hot_stocks")

# stock_zt_pool_em 列索引（涨停股池）
# 列顺序: 序号, 代码, _, 名称, 最新价, 涨跌幅, 成交额, 流通市值, 总市值, 连板数, 封单, 首次封板, 最后封板, 封单资金, 炸板次数, 所属行业, 涨停统计


def download_hot_stocks(
    store: SignalStore,
    start: str = "2019-01-01",
    end: str | None = None,
    batch_delay: float = 0.3,
) -> int:
    """下载强势股（东方财富涨停池），写入 SignalStore。

    注意：东方财富涨停池API仅保留最近数个交易日数据。
    历史周期（2019起）的涨停数据需在L2层从 daily_bars 计算。
    本函数只下载API可及的近期数据。
    返回写入行数。
    """
    from datetime import datetime
    end = end or datetime.now().strftime("%Y-%m-%d")

    total_rows = 0
    d = datetime.strptime(end, "%Y-%m-%d")
    end_dt = d
    # 尝试最近 60 个自然日（约 40 个交易日）
    from datetime import timedelta
    for i in range(60):
        d_str = d.strftime("%Y%m%d")
        if d_str < start.replace("-", ""):
            break
        try:
            df = safe_request(ak.stock_zt_pool_em, date=d_str)
            if df is not None and not df.empty:
                records = []
                for _, row in df.iterrows():
                    symbol = str(row.iloc[1]).strip()
                    industry = str(row.iloc[15]) if len(row) > 15 and pd.notna(row.iloc[15]) else ""
                    records.append({
                        "date": d.strftime("%Y-%m-%d"),
                        "symbol": symbol,
                        "reason_tags": industry,
                    })
                if records:
                    out = pd.DataFrame(records)
                    store.store_hot_stocks(out)
                    total_rows += len(out)
        except Exception:
            pass
        d -= timedelta(days=1)
        time.sleep(batch_delay)

    logger.info(f"强势股下载完成: {total_rows} 行 (日期范围 ~{end_dt.strftime('%Y-%m-%d')})")
    if total_rows == 0:
        logger.info("提示: 历史涨停数据需在引擎层从 daily_bars 计算获得")
    return total_rows
