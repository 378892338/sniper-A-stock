"""历史数据批量获取（指数 + 个股）— Sprint 5

统一数据入口：所有数据通过 DataStore 取用，日线是唯一事实来源，
周线/月线由 DataStore 按统一规则 resample 生成。
"""

import time
from pathlib import Path

import pandas as pd
import numpy as np

from data.store import DataStore
from core.logger import get_logger

logger = get_logger("backtest.data_loader")

# 三大市场指数
MARKET_INDEX_CODES = {
    "shanghai":  "sh000001",
    "shenzhen":  "sz399001",
    "chinext":   "sz399006",
    "csi300":    "sh000300",
}

# ETF分类指数
ETF_INDEX_CODES = {
    "证券":     "sz399975",
    "银行":     "sz399986",
    "军工":     "sz399967",
    "新能源车": "sz399976",
    "消费":     "sh000932",
    "医药":     "sh000933",
    "酒":       "sz399997",
    "有色":     "sh000819",
    "煤炭":     "sz399998",
    "半导体":   "sz399678",
    "光伏":     "sz399395",
    "科技":     "sz399440",
    "汽车":     "sz399432",
}


def fetch_index_daily(symbol: str, start: str = "2019-01-01",
                      end: str = "2024-12-31") -> pd.DataFrame:
    """获取指数日线 — 通过 DataSource 抽象"""
    from data.sources import get_source
    ds = get_source()
    try:
        df = ds.fetch_index_daily(symbol, start, end)
    except Exception as e:
        logger.warning(f"获取 {symbol} 失败: {e}")
        return pd.DataFrame()

    if df.empty:
        return df

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            if col == "volume":
                df[col] = 1e8
            else:
                df[col] = np.nan

    return df.sort_index()


def fetch_all_index_data(
    start: str = "2019-01-01",
    end: str = "2024-12-31",
    sleep: float = 2.0,
) -> DataStore:
    """获取所有指数日线数据，返回 DataStore（周线/月线由 DataStore 按需生成）"""
    store = DataStore()
    logger.info(f"开始获取指数数据: {start} ~ {end}")

    # 市场指数
    logger.info("--- 市场指数 ---")
    for name, code in MARKET_INDEX_CODES.items():
        if sleep > 0 and store.names:
            time.sleep(sleep)
        daily = fetch_index_daily(code, start, end)
        if not daily.empty:
            store.add_daily(name, daily)
            logger.info(f"  {name} ({code}): {len(daily)} 条日线")

    # ETF分类指数
    logger.info("--- ETF分类指数 ---")
    for name, code in ETF_INDEX_CODES.items():
        if sleep > 0:
            time.sleep(sleep)
        daily = fetch_index_daily(code, start, end)
        if not daily.empty:
            store.add_daily(name, daily)
            logger.info(f"  {name} ({code}): {len(daily)} 条日线")

    total_market = sum(1 for n in MARKET_INDEX_CODES if store.get_daily(n) is not None)
    total_etf = sum(1 for n in ETF_INDEX_CODES if store.get_daily(n) is not None)
    logger.info(f"获取完成: {total_market}/{len(MARKET_INDEX_CODES)} 市场指数, {total_etf}/{len(ETF_INDEX_CODES)} ETF指数")
    return store


def fetch_stock_pool(max_stocks: int = 100) -> list[str]:
    """获取股票池 — 通过 pre_filter + DataSource 抽象"""
    from data.downloader import DataDownloader

    dl = DataDownloader()
    stock_list = dl.fetch_stock_list()

    if stock_list.empty:
        logger.error("获取股票列表失败（所有数据源不可用）")
        return []

    stock_list = stock_list[stock_list["symbol"].str.match(r"^(00|30|60)\d{4}")]
    logger.info(f"  过滤板块后: {len(stock_list)} 只")

    symbols = stock_list["symbol"].head(max_stocks).tolist()
    logger.warning(f"⚠ 幸存者偏差: 当前仅获取现存股票，退市股票({max_stocks}只以外)未参与回测，收益可能系统性高估")
    logger.info(f"  最终股票池: {len(symbols)} 只")
    return symbols


def fetch_stocks_daily(symbols: list[str], start: str = "2019-01-01",
                       end: str = "2024-12-31", sleep: float = 2.5) -> dict[str, pd.DataFrame]:
    """批量获取个股日线（使用 fetch_daily_tx 腾讯源）"""
    from data.fetch import fetch_daily_tx

    result = {}
    success = 0
    failed = 0

    for i, sym in enumerate(symbols):
        if sleep > 0 and i > 0:
            time.sleep(sleep)

        try:
            df = fetch_daily_tx(sym, start, end)
            if not df.empty:
                result[sym] = df
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1

        if (i + 1) % 20 == 0:
            logger.info(f"  进度: {i+1}/{len(symbols)} (成功 {success}, 失败 {failed})")

    logger.info(f"个股获取完成: {success} 成功, {failed} 失败")
    return result


def load_all_from_cache(cache_dir: str | Path, n_stocks: int = 0) -> DataStore:
    """从缓存 parquet 加载所有日线，返回 DataStore

    n_stocks=0 表示全部加载。
    周线/月线由 DataStore 按需从日线 resample 生成并缓存。
    """
    return DataStore.from_parquet_cache(cache_dir, n_stocks=n_stocks)


def get_benchmark(store: DataStore) -> pd.Series | None:
    """从 DataStore 提取沪深300 close 作为基准"""
    daily = store.get_daily("csi300")
    if daily is not None and not daily.empty:
        return daily["close"]
    return None


def main():
    """独立下载：从 DataSource 获取全量数据并存入 parquet 缓存"""
    import argparse

    parser = argparse.ArgumentParser(description="下载回测数据到缓存")
    parser.add_argument("--stocks", type=int, default=100, help="个股数量")
    parser.add_argument("--sleep", type=float, default=2.5, help="请求间隔(秒)")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2024-12-31")
    args = parser.parse_args()

    cache_dir = Path("data/raw/_cache/backtest")
    cache_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=== 开始下载指数数据 ===")
    store = fetch_all_index_data(args.start, args.end, sleep=min(args.sleep, 2.0))

    logger.info("=== 开始下载个股数据 ===")
    symbols = fetch_stock_pool(max_stocks=args.stocks)
    stock_data = fetch_stocks_daily(symbols, args.start, args.end, sleep=args.sleep)
    for sym, df in stock_data.items():
        store.add_daily(sym, df)

    # 写入缓存
    store.to_parquet_cache(cache_dir)
    # 同时保存 benchmark 快照
    bm = get_benchmark(store)
    if bm is not None:
        bm.to_frame("close").to_parquet(cache_dir / "benchmark_csi300.parquet")

    n_stocks = len(store.stock_names)
    n_indices = len(store.index_names)
    logger.info(f"下载完成: {n_stocks} 个股, {n_indices} 指数")


if __name__ == "__main__":
    main()
