"""下载个股资金流向 — 同花顺数据源（主源，一次调用全市场）

优先使用同花顺源，因为 stock_fund_flow_individual(symbol="即时") 一次 API 调用
即可获取全市场所有股票的资金流数据，速度快（5000x 优于东方财富逐股下载）。

数据字段: 序号, 股票代码, 股票简称, 最新价, 涨跌幅, 换手率,
          流入资金, 流出资金, 净额, 成交额
"""

import time
import random
import akshare as ak
import pandas as pd

from sniper.signals.client import safe_request
from sniper.signals.store import SignalStore
from core.logger import get_logger

logger = get_logger("sniper.signals.download_fund_flow_10jqka")

# 同花顺个股资金流列索引（按位置）
COL_SYMBOL = 1    # 股票代码
COL_NET_INFLOW = 8  # 净额

# 非个股行前缀（如"今日主力净流入最大"等）
_SKIP_PREFIXES = ("今日", "近3", "近5", "近10", "近20", "行业")


def download_fund_flow_10jqka(
    store: SignalStore,
    symbols: list[str] | None = None,
    start: str = "2019-01-01",
    end: str | None = None,
) -> int:
    """用同花顺数据源下载全市场个股资金流向。

    调用 ak.stock_fund_flow_individual(symbol="即时") 一次获取全市场数据。
    同花顺源只返回当日数据，不涉及历史。
    返回值: 写入行数。

    Args:
        store: SignalStore 实例
        symbols: 过滤的股票列表（为 None 时不过滤，全部写入）
        start/end: 日期范围（同花顺只返回当天，该参数兼容接口签名）
    """
    from datetime import datetime
    target_date = end or datetime.now().strftime("%Y-%m-%d")

    # 随机延时 1~3s，防反爬
    delay = random.uniform(1.0, 3.0)
    logger.info(f"同花顺全市场请求前等待 {delay:.1f}s...")
    time.sleep(delay)

    df = safe_request(ak.stock_fund_flow_individual, symbol="即时", max_retries=3, delay=2.0)
    if df is None or df.empty:
        logger.warning("同花顺资金流: 无数据")
        return 0

    logger.info(f"同花顺全市场: {len(df)} 行原始数据")
    logger.info(f"  列名: {df.columns.tolist()}")

    # 解析数据
    rows = []
    for _, row in df.iterrows():
        try:
            sym = str(row.iloc[COL_SYMBOL]).strip()

            # 过滤非个股行
            if any(sym.startswith(p) for p in _SKIP_PREFIXES):
                continue
            if not sym.isdigit() or len(sym) != 6:
                continue

            # 过滤 symbol 列表
            if symbols and sym not in symbols:
                continue

            net_raw = row.iloc[COL_NET_INFLOW]
            net = _parse_net_flow(net_raw)

            rows.append({
                "symbol": sym,
                "date": target_date,
                "main_net": net,
                "retail_net": 0.0,
                "super_large": 0.0,
                "large_net": 0.0,
                "medium_net": 0.0,
                "small_net": 0.0,
            })
        except Exception:
            continue

    if not rows:
        logger.warning("同花顺资金流: 解析后无有效行")
        return 0

    out = pd.DataFrame(rows)
    store.store_fund_flow(out)
    logger.info(f"同花顺资金流: 写入 {len(out)} 行, 日期 {target_date}")
    return len(out)


def _parse_net_flow(val) -> float:
    """解析资金净额。

    支持格式:
      "1.23亿" → 1.23 * 100000000
      "5000万" → 5000 * 10000
      "12345678" → 12345678
      -12345678 → -12345678
      None/NaN → 0.0
    """
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val) if not pd.isna(val) else 0.0

    s = str(val).strip()
    if not s or s == "—" or s == "-":
        return 0.0

    sign = -1.0 if s.startswith("-") else 1.0
    s = s.lstrip("-+")

    if "亿" in s:  # 亿
        return sign * float(s.replace("亿", "")) * 100_000_000
    elif "万" in s:  # 万
        return sign * float(s.replace("万", "")) * 10_000
    else:
        try:
            return sign * float(s)
        except ValueError:
            return 0.0
