"""统一数据接口 — 封装 LocalDataWarehouse + SignalStore"""

import pandas as pd

from data.local.warehouse import LocalDataWarehouse
from sniper.signals.store import SignalStore
from core.logger import get_logger

logger = get_logger("sniper.data_router")


class DataRouter:
    """引擎层唯一数据源，屏蔽底层存储细节。
    所有引擎组件通过此接口读取数据，不直接访问 SQLite。
    """

    def __init__(self):
        self.wh = LocalDataWarehouse()
        self.sig = SignalStore()

    # ── 行情数据 ──

    def get_stock_list(self, status: str = "active") -> pd.DataFrame:
        return self.wh.get_stock_list(status=status)

    def get_daily_bars(
        self, symbol: str, start: str | None = None, end: str | None = None
    ) -> pd.DataFrame:
        df = self.wh.get_daily_bars(symbol, start=start or "2000-01-01", end=end or "2099-12-31")
        if not df.empty:
            df = df.reset_index()
        return df

    def get_index_daily(self, name: str = "shanghai", start: str | None = None, end: str | None = None) -> pd.DataFrame:
        df = self.wh.get_index_daily(name, start=start or "2000-01-01", end=end or "2099-12-31")
        if not df.empty:
            return df.reset_index()
        return df

    def get_trading_dates(self, start: str = "", end: str = "") -> pd.DataFrame:
        conn = self.wh._connect()
        try:
            sql = "SELECT date FROM trade_calendar WHERE is_trading = 1"
            params: list[str] = []
            if start:
                sql += " AND date >= ?"
                params.append(start)
            if end:
                sql += " AND date <= ?"
                params.append(end)
            sql += " ORDER BY date"
            return pd.read_sql(sql, conn, params=params)
        finally:
            conn.close()

    # ── L0 市场数据 ──

    def get_market_trend(self, index_name: str = "shanghai", window: int = 20) -> pd.DataFrame:
        """获取市场趋势数据（指数 + MA）。"""
        df = self.get_index_daily(index_name)
        if df.empty or "close" not in df.columns:
            return pd.DataFrame()
        df = df.sort_values("date")
        df["ma"] = df["close"].rolling(window).mean()
        df["pct"] = df["close"].pct_change() * 100
        return df

    def get_market_volume(self, index_name: str = "shanghai", window: int = 20) -> pd.DataFrame:
        """获取市场量能数据。"""
        df = self.get_index_daily(index_name)
        if df.empty or "volume" not in df.columns:
            return pd.DataFrame()
        df = df.sort_values("date")
        df["volume_ma"] = df["volume"].rolling(window).mean()
        df["volume_ratio"] = df["volume"] / df["volume_ma"].replace(0, float("nan"))
        return df

    def get_market_breadth(self, date: str) -> dict:
        """获取某日市场宽度（上涨/下跌家数）。"""
        conn = self.wh._connect()
        try:
            bars = pd.read_sql(
                "SELECT symbol, open, close FROM daily_bars WHERE date = ? AND volume > 0",
                conn, params=(date,),
            )
        finally:
            conn.close()
        if bars.empty:
            return {"advance": 0, "decline": 0, "ratio": 0.5}
        advance = int((bars["close"] >= bars["open"]).sum())
        decline = int((bars["close"] < bars["open"]).sum())
        total = advance + decline or 1
        return {"advance": advance, "decline": decline, "ratio": advance / total}

    def get_northbound_net(self, start: str, end: str) -> pd.DataFrame:
        """获取北向资金日净流入。"""
        return self.sig.get_northbound_daily(start=start, end=end)

    # ── L1 板块数据 ──

    def get_industry_compare(self, date: str) -> pd.DataFrame:
        """获取某日行业排名。"""
        return self.sig.get_industry_compare(date)

    def get_industry_compare_range(self, start: str, end: str) -> pd.DataFrame:
        return self.sig.get_industry_compare_range(start, end)

    def get_industry_compare_sw1(self, date: str) -> pd.DataFrame:
        return self.sig.get_industry_compare_sw1(date)

    def get_industry_compare_sw2(self, date: str) -> pd.DataFrame:
        return self.sig.get_industry_compare_sw2(date)

    def get_industry_compare_sw1_range(self, start: str, end: str) -> pd.DataFrame:
        return self.sig.get_industry_compare_sw1_range(start, end)

    def get_industry_compare_sw2_range(self, start: str, end: str) -> pd.DataFrame:
        return self.sig.get_industry_compare_sw2_range(start, end)

    def get_hot_stocks(self, date: str) -> pd.DataFrame:
        """获取某日强势股/题材。"""
        return self.sig.get_hot_stocks(date)

    # ── L2 个股数据 ──

    def get_fund_flow(self, symbol: str, start: str = "", end: str = "") -> pd.DataFrame:
        return self.sig.get_fund_flow(symbol, start=start, end=end)

    def get_fund_flow_batch(self, symbols: list[str], date: str) -> pd.DataFrame:
        return self.sig.get_fund_flow_batch(symbols, date)

    def get_dragon_tiger(self, date: str) -> pd.DataFrame:
        return self.sig.get_dragon_tiger(date)

    def get_quarterly(self, symbol: str, report_date: str = "") -> pd.DataFrame:
        return self.sig.get_quarterly(symbol, report_date)

    def get_latest_quarterly_batch(self, symbols: list[str], as_of: str) -> pd.DataFrame:
        return self.sig.get_latest_quarterly_batch(symbols, as_of)

    # ── ETF 数据接口 — 新增(L0.5 ETF动量层) ──

    def get_etf_daily(self, etf_name: str, start: str = "", end: str = "") -> pd.DataFrame:
        """获取单个ETF分类指数日线。

        委托给 data/index_etf.py, 数据缓存在外部。
        返回 DataFrame[date, open, high, low, close, volume], 空时表示数据不可达。
        """
        from data.index_etf import fetch_etf_index_data
        df = fetch_etf_index_data(etf_name, start or "2000-01-01", end or "2099-12-31")
        if df.empty or "close" not in df.columns:
            return pd.DataFrame()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).astype(str)
        return df

    def get_etf_daily_batch(self, etf_names: list[str] | None = None,
                            start: str = "", end: str = "") -> dict[str, pd.DataFrame]:
        """批量获取多个ETF分类指数日线。

        使用已有线程池并发获取, 单个失败不影响其他。
        Returns: {etf_name: DataFrame} — 获取失败的ETF不在dict中
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from data.index_etf import ETF_INDEX_MAP

        if etf_names is None:
            etf_names = list(ETF_INDEX_MAP.keys())

        result: dict[str, pd.DataFrame] = {}
        max_workers = min(8, len(etf_names))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.get_etf_daily, name, start, end): name
                for name in etf_names
            }
            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    df = future.result(timeout=30)
                    if not df.empty:
                        result[name] = df
                except Exception as e:
                    logger.warning(f"ETF[{name}] 获取失败: {e}")
        logger.info(f"ETF数据: 成功{len(result)}/{len(etf_names)}")
        return result

    # ── 交易日工具 ──

    def get_prev_trading_day(self, date: str) -> str | None:
        """获取上一个交易日。"""
        df = self.get_trading_dates()
        if df.empty or "date" not in df.columns:
            return None
        dates = sorted(df["date"].tolist())
        for i in range(len(dates) - 1, -1, -1):
            if dates[i] < date:
                return dates[i]
        return None

    def get_trading_days_before(self, date: str, n: int) -> list[str]:
        """获取 date 前 n 个交易日列表。"""
        df = self.get_trading_dates()
        if df.empty or "date" not in df.columns:
            return []
        dates = sorted(df["date"].tolist())
        if date not in dates:
            return []
        idx = dates.index(date)
        return dates[max(0, idx - n + 1): idx + 1]
