"""下载龙虎榜数据 — a-stock-data / akshare 东方财富源"""

import time
import akshare as ak
import pandas as pd

from sniper.signals.client import safe_request
from sniper.signals.store import SignalStore
from core.logger import get_logger

logger = get_logger("sniper.signals.download_dragon_tiger")

# stock_lhb_detail_em 列索引
COL_CODE = 1       # 代码
COL_DATE = 3       # 上榜日期
COL_NET_BUY = 7    # 龙虎榜净买额
COL_BUY_AMT = 8    # 龙虎榜买入额
COL_SELL_AMT = 9   # 龙虎榜卖出额
COL_TURNOVER = 14  # 换手率
COL_REASON = 16    # 上榜原因


def download_dragon_tiger(
    store: SignalStore,
    start: str = "2019-01-01",
    end: str | None = None,
    batch_delay: float = 1.0,
) -> int:
    """下载龙虎榜数据（东方财富源），写入 SignalStore。

    按月分批获取全市场龙虎榜。
    返回写入行数。
    """
    from datetime import datetime, timedelta
    end = end or datetime.now().strftime("%Y-%m-%d")

    # 按月分批
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    months = []
    cursor = start_dt
    while cursor <= end_dt:
        month_end = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        months.append((cursor.strftime("%Y%m%d"), min(month_end, end_dt).strftime("%Y%m%d")))
        cursor = month_end + timedelta(days=1)

    logger.info(f"下载龙虎榜: {len(months)} 个月份")

    total_rows = 0
    for i, (s, e) in enumerate(months):
        try:
            df = safe_request(ak.stock_lhb_detail_em, start_date=s, end_date=e)
            if df is None or df.empty:
                continue

            rows = []
            for _, row in df.iterrows():
                rows.append({
                    "date": str(row.iloc[COL_DATE]),
                    "symbol": str(row.iloc[COL_CODE]).strip(),
                    "reason": str(row.iloc[COL_REASON]) if pd.notna(row.iloc[COL_REASON]) else "",
                    "net_buy": float(row.iloc[COL_NET_BUY]) if pd.notna(row.iloc[COL_NET_BUY]) else 0.0,
                    "buy_amount": float(row.iloc[COL_BUY_AMT]) if pd.notna(row.iloc[COL_BUY_AMT]) else 0.0,
                    "sell_amount": float(row.iloc[COL_SELL_AMT]) if pd.notna(row.iloc[COL_SELL_AMT]) else 0.0,
                    "institution_buy": 0.0,
                    "institution_sell": 0.0,
                    "turnover_rate": float(row.iloc[COL_TURNOVER]) if pd.notna(row.iloc[COL_TURNOVER]) else 0.0,
                })

            if rows:
                out = pd.DataFrame(rows)
                # 同一天同一只股票可能因多个原因上榜，按 date+symbol 聚合
                agg = out.groupby(["date", "symbol"], as_index=False).agg({
                    "reason": lambda x: " | ".join(filter(None, x.dropna().unique())),
                    "net_buy": "sum", "buy_amount": "sum",
                    "sell_amount": "sum", "turnover_rate": "mean",
                    "institution_buy": "sum", "institution_sell": "sum",
                })
                store.store_dragon_tiger(agg)
                total_rows += len(agg)

        except Exception as e:
            logger.warning(f"龙虎榜下载失败 {s}~{e}: {e}")

        time.sleep(batch_delay)

        if len(months) > 12:
            step = max(len(months) // 5, 1)
            if (i + 1) % step == 0:
                pct = (i + 1) / len(months) * 100
                logger.info(f"龙虎榜进度: {pct:.0f}% ({total_rows} 行)")

    logger.info(f"龙虎榜下载完成: {total_rows} 行")
    return total_rows
