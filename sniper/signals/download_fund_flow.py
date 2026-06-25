"""下载个股资金流向 — akshare 东方财富源（仅最近约6个月）

反爬措施:
  - 随机延时 0.1~0.5s（避免固定模式）
  - 10% 概率 1~3s 长暂停
  - 每 500 只 5~10s 冷却
"""

import time
import random
import akshare as ak
import pandas as pd

from sniper.signals.client import safe_request
from sniper.signals.store import SignalStore
from core.logger import get_logger

logger = get_logger("sniper.signals.download_fund_flow")

# 东方财富个股资金流向列索引（按位置，避免编码问题）
COL_DATE = 0      # 日期
COL_MAIN_NET = 3  # 主力净流入-净额
COL_SUPER = 5     # 超大单净流入-净额
COL_LARGE = 7     # 大单净流入-净额
COL_MEDIUM = 9    # 中单净流入-净额
COL_SMALL = 11    # 小单净流入-净额


def download_fund_flow(
    store: SignalStore,
    symbols: list[str] | None = None,
    start: str = "2019-01-01",
    end: str | None = None,
    batch_size: int = 100,
    delay: float = 0.2,
) -> int:
    """下载个股资金流向（东方财富源），写入 SignalStore。

    注意：东方财富 API 仅返回最近约 6 个月数据，2019 年前无数据。
    用 ak.stock_individual_fund_flow 逐股获取，单次重试。
    返回写入行数。
    """
    from datetime import datetime
    end = end or datetime.now().strftime("%Y-%m-%d")

    if symbols is None:
        from data.local.warehouse import LocalDataWarehouse
        wh = LocalDataWarehouse()
        stock_df = wh.get_stock_list(status="active")
        symbols = stock_df["symbol"].tolist() if not stock_df.empty else []
        logger.info(f"全市场 {len(symbols)} 只股票")

    total_rows = 0
    skipped = 0
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        for sym in batch:
            try:
                market = "sh" if sym.startswith(("6", "9")) else "sz"
                df = safe_request(ak.stock_individual_fund_flow, stock=sym, market=market, max_retries=2, delay=1.0)
                if df is None or df.empty:
                    skipped += 1
                    continue

                rows = []
                for _, row in df.iterrows():
                    d = row.iloc[COL_DATE]
                    if hasattr(d, "strftime"):
                        d_str = d.strftime("%Y-%m-%d")
                    else:
                        d_str = str(d)
                    if d_str < start or d_str > end:
                        continue
                    rows.append({
                        "symbol": sym, "date": d_str,
                        "main_net": float(row.iloc[COL_MAIN_NET]) if pd.notna(row.iloc[COL_MAIN_NET]) else 0.0,
                        "super_large": float(row.iloc[COL_SUPER]) if pd.notna(row.iloc[COL_SUPER]) else 0.0,
                        "large_net": float(row.iloc[COL_LARGE]) if pd.notna(row.iloc[COL_LARGE]) else 0.0,
                        "medium_net": float(row.iloc[COL_MEDIUM]) if pd.notna(row.iloc[COL_MEDIUM]) else 0.0,
                        "small_net": float(row.iloc[COL_SMALL]) if pd.notna(row.iloc[COL_SMALL]) else 0.0,
                    })

                if rows:
                    out = pd.DataFrame(rows)
                    out["retail_net"] = out["small_net"]
                    out = out[["symbol", "date", "main_net", "retail_net",
                               "super_large", "large_net", "medium_net", "small_net"]]
                    store.store_fund_flow(out)
                    total_rows += len(out)

            except Exception:
                skipped += 1

            # 随机延时 0.1~0.5s + 10% 概率长暂停 1~3s
            d = random.uniform(0.1, 0.5)
            if random.random() < 0.1:
                d += random.uniform(1.0, 3.0)
            time.sleep(d)

            if len(symbols) > 500 and (i + len(batch)) % 500 == 0:
                cool = random.uniform(5.0, 10.0)
                logger.info(f"反爬冷却 {cool:.0f}s...")
                time.sleep(cool)
                logger.info(f"资金流向进度: {min(i + batch_size, len(symbols))}/{len(symbols)}, "
                            f"已写入 {total_rows} 行, 跳过 {skipped}")

    logger.info(f"资金流向下载完成: {total_rows} 行, 跳过 {skipped}")
    return total_rows
