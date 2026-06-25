"""统一数据下载模块 — 在 pre_filter 之后、L1 之前运行，支持周期更新"""

import time
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from core.logger import get_logger
from shared.cache import read_cache, write_cache

logger = get_logger("data.downloader")

# 三大市场指数代码
MARKET_INDEX_CODES = {
    "shanghai": "000001",
    "shenzhen": "399001",
    "chinext":  "399006",
    "csi300":   "000300",
}

# ETF 分类指数
ETF_INDEX_CODES = {
    "证券": "399975", "银行": "399986", "军工": "399967",
    "新能源车": "399976", "消费": "000932", "医药": "000933",
    "酒": "399997", "有色": "000819", "煤炭": "399998",
}

# 默认 TTL（秒）
TTL_MARKET_DAILY = 3600       # 大盘日线: 1小时
TTL_MARKET_WEEKLY = 86400     # 大盘周线: 1天
TTL_ETF_DAILY = 3600
TTL_STOCK_DAILY = 86400
TTL_STOCK_LIST = 86400 * 7    # 股票列表: 7天
TTL_FUND_FLOW = 3600


class DataDownloader:
    """统一数据下载器 — 通过 DataSource 抽象获取全部数据，TTL 缓存"""

    def __init__(self):
        self._last_refresh: dict[str, float] = {}

    # ── 市场指数 ──

    def fetch_market_index(self, code: str, name: str, start: str, end: str,
                           ttl: int = TTL_MARKET_DAILY) -> pd.DataFrame:
        """获取单个市场指数日线"""
        cache_name = f"market_daily_{name}"
        cached = read_cache(cache_name, start, end, ttl_seconds=ttl)
        if cached is not None:
            return cached

        from data.sources import get_source
        ds = get_source()
        try:
            df = ds.fetch_index_daily(code, start, end)
            if not df.empty:
                write_cache(df, cache_name, start, end)
                self._last_refresh[f"market_{name}"] = time.time()
            return df
        except Exception as e:
            logger.warning(f"获取市场指数 {name}({code}) 失败: {e}")
            return pd.DataFrame()

    def fetch_all_market(self, start: str, end: str) -> dict:
        """获取全部市场指数（并发）"""
        result = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {
                ex.submit(self.fetch_market_index, code, name, start, end): name
                for name, code in MARKET_INDEX_CODES.items()
            }
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    df = fut.result()
                    if not df.empty:
                        result[name] = df
                except Exception as e:
                    logger.warning(f"市场指数 {name} 并发获取失败: {e}")
        logger.info(f"市场指数: {len(result)}/{len(MARKET_INDEX_CODES)} 获取成功")
        return result

    def resample_to_weekly(self, daily: pd.DataFrame) -> pd.DataFrame:
        if daily.empty:
            return daily
        return daily.resample("W-FRI", closed="right", label="right").agg({
            "open": "first", "close": "last",
            "high": "max", "low": "min", "volume": "sum",
        }).dropna()

    def resample_to_monthly(self, daily: pd.DataFrame) -> pd.DataFrame:
        if daily.empty:
            return daily
        return daily.resample("ME", closed="right", label="right").agg({
            "open": "first", "close": "last",
            "high": "max", "low": "min", "volume": "sum",
        }).dropna()

    # ── ETF 分类指数 ──

    def fetch_etf_index(self, code: str, name: str, start: str, end: str,
                        ttl: int = TTL_ETF_DAILY) -> pd.DataFrame:
        """获取单个 ETF 分类指数日线"""
        cache_name = f"etf_daily_{name}"
        cached = read_cache(cache_name, start, end, ttl_seconds=ttl)
        if cached is not None:
            return cached

        from data.sources import get_source
        ds = get_source()
        try:
            df = ds.fetch_index_daily(code, start, end)
            if not df.empty:
                write_cache(df, cache_name, start, end)
                self._last_refresh[f"etf_{name}"] = time.time()
            return df
        except Exception as e:
            logger.warning(f"获取ETF指数 {name}({code}) 失败: {e}")
            return pd.DataFrame()

    def fetch_all_etf(self, start: str, end: str) -> dict:
        """获取全部 ETF 分类指数（并发）"""
        result = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {
                ex.submit(self.fetch_etf_index, code, name, start, end): name
                for name, code in ETF_INDEX_CODES.items()
            }
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    df = fut.result()
                    if not df.empty:
                        result[name] = df
                except Exception as e:
                    logger.warning(f"ETF指数 {name} 并发获取失败: {e}")
        logger.info(f"ETF指数: {len(result)}/{len(ETF_INDEX_CODES)} 获取成功")
        return result

    # ── 股票列表 ──

    def fetch_stock_list(self, ttl: int = TTL_STOCK_LIST) -> pd.DataFrame:
        """获取全 A 股列表（通过 pre_filter 过滤后）"""
        cache_name = "stock_list_filtered"
        cached = read_cache(cache_name, ttl_seconds=ttl)
        if cached is not None:
            return cached

        from data.pre_filter import run_pre_filter
        from data.sources import get_source

        ds = get_source()
        try:
            # 先获取原始列表
            import akshare as ak
            raw = ak.stock_zh_a_spot_em()
            raw = raw.rename(columns={"代码": "symbol", "名称": "name"})
            raw["symbol"] = raw["symbol"].astype(str).str.zfill(6)
        except Exception as e:
            logger.warning(f"获取股票列表失败: {e}")
            return pd.DataFrame()

        # 运行前置过滤
        df = run_pre_filter(raw)
        if not df.empty:
            write_cache(df, cache_name)
            self._last_refresh["stock_list"] = time.time()
        return df

    # ── 资金流数据 ──

    def fetch_fund_flow(self, start: str, end: str,
                        ttl: int = TTL_FUND_FLOW) -> dict:
        """获取资金流数据（北向/融资/大单）"""
        cache_name = "fund_flow_data"
        cached = read_cache(cache_name, start, end, ttl_seconds=ttl)
        if cached is not None:
            return cached.to_dict() if hasattr(cached, 'to_dict') else cached

        from data.sources import get_fundflow_source
        fund_data = {}
        try:
            ff = get_fundflow_source()
            if ff.is_available():
                nb = ff.fetch_northbound_flow(start, end)
                if nb is not None:
                    fund_data["northbound"] = nb
        except Exception as e:
            logger.warning(f"获取资金流数据失败: {e}")

        if fund_data:
            self._last_refresh["fund_flow"] = time.time()
        return fund_data

    # ── 完整数据包 ──

    def fetch_full_data_pack(self, start: str = None, end: str = None,
                             include_stocks: bool = False,
                             stock_symbols: list[str] = None) -> dict:
        """
        拉取完整数据包：市场指数 + ETF指数 + 资金流（+ 可选个股）。

        返回:
        {
            "market_daily": {name: df},
            "market_weekly": {name: df},
            "market_monthly": {name: df},
            "etf_daily": {name: df},
            "etf_weekly": {name: df},
            "benchmark": pd.Series | None,   # 沪深300日线 close
            "fund_data": dict,
            "stock_data": {sym: df} | None,  # 仅 include_stocks=True
            "stock_list": pd.DataFrame | None,
        }
        """
        if end is None:
            end = datetime.now().strftime("%Y-%m-%d")
        if start is None:
            start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")

        logger.info(f"开始拉取完整数据包: {start} ~ {end}")

        # 市场指数
        market_daily = self.fetch_all_market(start, end)
        market_weekly = {n: self.resample_to_weekly(d) for n, d in market_daily.items()}
        market_monthly = {n: self.resample_to_monthly(d) for n, d in market_daily.items()}

        benchmark = None
        if "csi300" in market_daily and not market_daily["csi300"].empty:
            benchmark = market_daily["csi300"]["close"]

        # ETF 指数
        etf_daily = self.fetch_all_etf(start, end)
        etf_weekly = {n: self.resample_to_weekly(d) for n, d in etf_daily.items()}

        # 资金流
        fund_data = self.fetch_fund_flow(start, end)

        # 股票列表（经 pre_filter 过滤）
        stock_list = self.fetch_stock_list()

        # 可选：个股数据
        stock_data = None
        if include_stocks and stock_symbols:
            stock_data = self._fetch_stocks_batch(stock_symbols, start, end)

        logger.info(
            f"数据包完成: 市场{len(market_daily)} ETF{len(etf_daily)} "
            f"股票列表{len(stock_list) if stock_list is not None else 0}"
        )

        return {
            "market_daily": market_daily,
            "market_weekly": market_weekly,
            "market_monthly": market_monthly,
            "etf_daily": etf_daily,
            "etf_weekly": etf_weekly,
            "benchmark": benchmark,
            "fund_data": fund_data,
            "stock_data": stock_data,
            "stock_list": stock_list,
        }

    def _fetch_stocks_batch(self, symbols: list[str], start: str,
                            end: str) -> dict[str, pd.DataFrame]:
        """批量获取个股日线"""
        from data.sources import get_source
        ds = get_source()
        result = {}
        for sym in symbols:
            try:
                df = ds.fetch_daily(sym, start, end)
                if not df.empty:
                    result[sym] = df
            except Exception as e:
                logger.debug(f"获取个股 {sym} 失败: {e}")
        return result

    # ── 周期更新 ──

    def needs_refresh(self, data_type: str, ttl: int = 3600) -> bool:
        """检查指定数据类型是否需要刷新"""
        last = self._last_refresh.get(data_type)
        if last is None:
            return True
        return (time.time() - last) > ttl

    def refresh_if_needed(self, data_type: str, ttl: int = 3600,
                          start: str = None, end: str = None) -> bool:
        """按需刷新 — 返回 True 表示执行了刷新"""
        if not self.needs_refresh(data_type, ttl):
            return False
        logger.info(f"数据刷新: {data_type} (TTL={ttl}s)")
        return True

    def get_last_refresh(self, data_type: str = None) -> float | dict:
        """获取最后刷新时间"""
        if data_type:
            return self._last_refresh.get(data_type, 0)
        return dict(self._last_refresh)

    def force_refresh_all(self, start: str = None, end: str = None):
        """强制刷新全部数据（忽略 TTL）"""
        self._last_refresh.clear()
        return self.fetch_full_data_pack(start, end, include_stocks=False)
