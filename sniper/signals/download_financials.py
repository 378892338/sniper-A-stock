"""下载季报财务数据 — akshare 同花顺财务指标源"""

import time
import akshare as ak
import pandas as pd

from sniper.signals.client import safe_request
from sniper.signals.store import SignalStore
from core.logger import get_logger

logger = get_logger("sniper.signals.download_financials")

# stock_financial_abstract_ths 列索引（按报告期）
COL_RP = 0     # 报告期
COL_NP = 1     # 净利润
COL_NPY = 2    # 净利润同比增长
COL_REV = 5    # 营业总收入
COL_REVY = 6   # 营业总收入同比增长
COL_EPS = 7    # 基本每股收益
COL_CF = 11    # 每股经营现金流
COL_NPM = 12   # 销售净利率（作为毛利率替代）
COL_ROE = 13   # 净资产收益率
COL_DA = 21    # 资产负债率


def download_quarterly(
    store: SignalStore,
    symbols: list[str] | None = None,
    batch_size: int = 100,
    delay: float = 0.3,
) -> int:
    """下载季报财务数据（同花顺财务指标源），写入 SignalStore。

    逐股获取历史财务指标。
    返回写入行数。
    """
    if symbols is None:
        from data.local.warehouse import LocalDataWarehouse
        wh = LocalDataWarehouse()
        stock_df = wh.get_stock_list(status="active")
        symbols = stock_df["symbol"].tolist() if not stock_df.empty else []
        logger.info(f"全市场 {len(symbols)} 只股票")

    def _clean(val) -> float:
        """清理 akshare 返回值中的中文单位/百分号并转为 float。"""
        if pd.isna(val):
            return 0.0
        s = str(val).replace(",", "").replace("，", "").strip()
        if not s:
            return 0.0
        s = s.replace("%", "").replace("亿", "").replace("万", "").replace("元", "")
        s = s.replace("--", "0").replace("—", "0").replace("-", "0")
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    total_rows = 0
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        for sym in batch:
            try:
                df = safe_request(
                    ak.stock_financial_abstract_ths,
                    symbol=sym,
                    indicator="按报告期",
                )
                if df is None or df.empty:
                    continue

                rows = []
                for _, row in df.iterrows():
                    rows.append({
                        "symbol": sym,
                        "report_date": str(row.iloc[COL_RP]),
                        "eps": _clean(row.iloc[COL_EPS]),
                        "roe": _clean(row.iloc[COL_ROE]),
                        "net_profit": _clean(row.iloc[COL_NP]),
                        "net_profit_yoy": _clean(row.iloc[COL_NPY]),
                        "revenue": _clean(row.iloc[COL_REV]),
                        "revenue_yoy": _clean(row.iloc[COL_REVY]),
                        "gross_margin": _clean(row.iloc[COL_NPM]),
                        "pe_ttm": 0.0,
                        "pb": 0.0,
                        "debt_to_assets": _clean(row.iloc[COL_DA]),
                        "free_cash_flow": _clean(row.iloc[COL_CF]),
                    })

                if rows:
                    out = pd.DataFrame(rows)
                    out = out[["symbol", "report_date", "eps", "roe", "net_profit",
                               "net_profit_yoy", "revenue", "revenue_yoy", "gross_margin",
                               "pe_ttm", "pb", "debt_to_assets", "free_cash_flow"]]
                    store.store_quarterly(out)
                    total_rows += len(out)

            except Exception as e:
                logger.warning(f"财务数据下载失败 {sym}: {e}")

            time.sleep(delay)

        logger.info(f"财务数据进度: {min(i + batch_size, len(symbols))}/{len(symbols)}, "
                    f"已写入 {total_rows} 行")

    logger.info(f"财务数据下载完成: {total_rows} 行")
    return total_rows
