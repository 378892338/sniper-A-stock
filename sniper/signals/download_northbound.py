"""下载北向资金流向 — a-stock-data / akshare 同花顺源"""

import akshare as ak
import pandas as pd

from sniper.signals.client import safe_request
from sniper.signals.store import SignalStore
from core.logger import get_logger

logger = get_logger("sniper.signals.download_northbound")


def download_northbound(
    store: SignalStore,
    start: str = "2019-01-01",
    end: str | None = None,
) -> int:
    """下载北向资金流向（东方财富源），写入 SignalStore。

    返回写入行数。
    """
    import datetime as dt
    end = end or dt.datetime.now().strftime("%Y-%m-%d")

    logger.info(f"下载北向资金: {start} ~ {end}")

    records = []
    for label, col_key in [("沪股通", "sh_net"), ("深股通", "sz_net")]:
        try:
            df = safe_request(ak.stock_hsgt_hist_em, symbol=label)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    d = row.iloc[0]
                    if hasattr(d, "strftime"):
                        d_str = d.strftime("%Y-%m-%d")
                    else:
                        d_str = str(d)
                    if d_str < start or d_str > end:
                        continue
                    records.append({
                        "date": d_str,
                        "time": "15:30",
                        col_key: float(row.iloc[1]),
                    })
        except Exception as e:
            logger.warning(f"{label} 北向数据下载失败: {e}")

    if not records:
        logger.warning("北向资金数据获取失败")
        return 0

    combined = pd.DataFrame(records)
    combined = combined.groupby("date", as_index=False).agg({
        "sh_net": "sum", "sz_net": "sum", "time": "first",
    })
    combined["total"] = combined["sh_net"] + combined["sz_net"]
    combined = combined.sort_values("date")[["date", "time", "sh_net", "sz_net", "total"]]

    store.store_northbound(combined)
    n = len(combined)
    logger.info(f"北向资金下载完成: {n} 条 (从 {combined['date'].min()} 至 {combined['date'].max()})")
    return n
