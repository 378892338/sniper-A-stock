"""下载行业对比数据 — 从 sw_index_daily 计算申万行业表现"""

import time
import akshare as ak
import pandas as pd
import numpy as np

from sniper.signals.client import safe_request
from sniper.signals.store import SignalStore
from core.logger import get_logger

logger = get_logger("sniper.signals.download_industry")

SW_INDEX_MAP = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁",
    "801050": "有色金属", "801080": "电子",      "801110": "家用电器",
    "801120": "食品饮料", "801130": "纺织服饰", "801140": "轻工制造",
    "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
    "801180": "房地产",   "801200": "商贸零售", "801210": "社服",
    "801230": "综合",     "801710": "建筑材料", "801720": "建筑装饰",
    "801730": "电力设备", "801740": "国防军工", "801750": "计算机",
    "801760": "传媒",     "801770": "通信",      "801780": "银行",
    "801790": "非银金融", "801880": "汽车",      "801890": "机械设备",
    "801950": "煤炭",     "801960": "石油石化", "801970": "环保",
    "801980": "美容护理",
}

# index_hist_sw 列索引（按位置，避免编码问题）
COL_CODE = 0     # 指数代码
COL_DATE = 1     # 日期
COL_OPEN = 2     # 开盘
COL_HIGH = 3     # 最高
COL_LOW = 4      # 最低
COL_CLOSE = 5    # 收盘
COL_VOLUME = 6   # 成交量


def _download_sw_index_data(
    start: str = "2019-01-01",
    end: str = "2026-05-13",
    delay: float = 0.3,
) -> int:
    """下载申万行业指数日线到 sw_index_daily 表。

    使用位置索引避免编码问题。
    返回写入行数。
    """
    from data.local.warehouse import LocalDataWarehouse
    wh = LocalDataWarehouse()

    total = 0
    for code, name in SW_INDEX_MAP.items():
        try:
            raw = safe_request(ak.index_hist_sw, symbol=code, period="day")
            if raw is None or raw.empty:
                continue

            rows_data = {
                "code": code,
                "date": [
                    d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
                    for d in raw.iloc[:, COL_DATE]
                ],
                "open": pd.to_numeric(raw.iloc[:, COL_OPEN], errors="coerce"),
                "high": pd.to_numeric(raw.iloc[:, COL_HIGH], errors="coerce"),
                "low": pd.to_numeric(raw.iloc[:, COL_LOW], errors="coerce"),
                "close": pd.to_numeric(raw.iloc[:, COL_CLOSE], errors="coerce"),
                "volume": pd.to_numeric(raw.iloc[:, COL_VOLUME], errors="coerce"),
            }
            df = pd.DataFrame(rows_data)

            mask = (df["date"] >= start) & (df["date"] <= end)
            df = df[mask]
            if df.empty:
                continue

            wh.store_sw_index_daily(
                df[["code", "date", "open", "high", "low", "close", "volume"]],
                if_exists="append",
            )
            total += len(df)
        except Exception as e:
            logger.warning(f"申万 {code}({name}) 下载失败: {e}")

        time.sleep(delay)

    logger.info(f"申万行业指数下载完成: {total} 行")
    return total


def download_industry_compare(
    store: SignalStore,
    start: str = "2019-01-01",
    end: str | None = None,
    board_delay: float = 0.3,
) -> int:
    """从 sw_index_daily 计算申万行业日表现，写入 industry_compare 表。

    自动触发行业指数数据下载（如尚未就绪）。
    返回写入行数。
    """
    from datetime import datetime
    end = end or datetime.now().strftime("%Y-%m-%d")

    # 1. 确保 sw_index_daily 有数据
    conn = store._connect()
    try:
        existing = pd.read_sql(
            "SELECT COUNT(*) as cnt FROM sw_index_daily WHERE date >= ? AND date <= ?",
            conn, params=(start, end),
        )
        has_data = existing.iloc[0, 0] > 0
    finally:
        conn.close()

    if not has_data:
        logger.info("sw_index_daily 无数据，开始下载申万行业指数...")
        _download_sw_index_data(start=start, end=end, delay=board_delay)

    # 2. 从 sw_index_daily 计算日涨跌幅 + 量能变化
    conn = store._connect()
    try:
        df = pd.read_sql(
            "SELECT code, date, close, volume FROM sw_index_daily "
            "WHERE date >= ? AND date <= ? ORDER BY code, date",
            conn, params=(start, end),
        )
    finally:
        conn.close()

    if df.empty:
        logger.warning("sw_index_daily 无数据，无法生成行业对比")
        return 0

    # 计算日涨跌幅
    df["daily_change"] = df.groupby("code")["close"].pct_change() * 100
    df = df.dropna(subset=["daily_change"])

    # 计算量能变化（实时从volume计算）
    df["volume_change"] = df.groupby("code")["volume"].pct_change() * 100
    df["volume_change"] = df["volume_change"].fillna(0).clip(-100, 500).round(1)

    df["industry_name"] = df["code"].map(SW_INDEX_MAP)
    df = df.dropna(subset=["industry_name"])

    if df.empty:
        logger.warning("行业涨跌幅计算为空")
        return 0

    # 加载行业映射获取各行业成分股数量
    try:
        from pathlib import Path as _P
        cache_dir = _P(__file__).resolve().parents[2] / "data/raw/_cache"
        ind_caches = sorted(cache_dir.glob("sw_industry_cons_*.parquet"))
        if ind_caches:
            ind_df = pd.read_parquet(ind_caches[0])
            if "industry" in ind_df.columns:
                stock_counts = ind_df["industry"].value_counts().to_dict()
            else:
                stock_counts = {}
        else:
            stock_counts = {}
    except Exception:
        stock_counts = {}

    out = df[["date", "industry_name", "daily_change", "volume_change"]].copy()
    out["leader_symbol"] = ""
    out["leader_change"] = 0.0
    out["stock_count"] = out["industry_name"].map(stock_counts).fillna(0).astype(int)

    out = out.sort_values(["date", "daily_change"], ascending=[True, False])
    out["rank"] = out.groupby("date").cumcount() + 1

    out = out[["date", "industry_name", "daily_change",
               "volume_change", "leader_symbol", "leader_change",
               "rank", "stock_count"]]

    store.store_industry_compare(out)
    n = len(out)
    logger.info(f"行业对比下载完成: {n} 行 (从 sw_index_daily 计算)")
    return n
