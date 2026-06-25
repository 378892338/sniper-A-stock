"""sniper 测试共享 mock 与 fixture"""

import pandas as pd
import pytest


class _MockCursor:
    """模拟 DB-API 2.0 cursor，供 pd.read_sql 使用。"""
    description = []

    @staticmethod
    def execute(*args, **kwargs):
        return _MockCursor()

    @staticmethod
    def fetchall():
        return []

    @staticmethod
    def close():
        pass


class _MockWh:
    """模拟 SQLite 连接，StockScorer.all_stocks 需要。"""
    class _Conn:
        @staticmethod
        def cursor():
            return _MockCursor()

        @staticmethod
        def close():
            pass

    @staticmethod
    def _connect():
        return _MockWh._Conn()


class MockDataRouter:
    """模拟 DataRouter，无需数据库即可测试各层逻辑。"""

    def __init__(self):
        self.wh = _MockWh()
        self._bars: dict[str, pd.DataFrame] = {}
        self._index_bars: dict[str, pd.DataFrame] = {}
        self._trading_dates: list[str] = []
        self._fund_flow_data: pd.DataFrame = pd.DataFrame()
        self._dragon_tiger_data: pd.DataFrame = pd.DataFrame()
        self._quarterly_data: pd.DataFrame = pd.DataFrame()
        self._industry_data: pd.DataFrame = pd.DataFrame()
        self._hot_stocks_data: pd.DataFrame = pd.DataFrame()
        self._northbound_data: pd.DataFrame = pd.DataFrame()
        self._breadth_data: dict = {"advance": 500, "decline": 300, "ratio": 0.625}

    def set_daily_bars(self, symbol: str, df: pd.DataFrame):
        self._bars[symbol] = df

    def set_index_bars(self, name: str, df: pd.DataFrame):
        self._index_bars[name] = df

    def get_daily_bars(self, symbol, start=None, end=None):
        df = self._bars.get(symbol, pd.DataFrame())
        if df.empty:
            return df
        df = df.copy()
        if isinstance(df.index, pd.DatetimeIndex) and "date" not in df.columns:
            df["date"] = df.index
        if start:
            col = "date" if "date" in df.columns else df.index
            df = df[df[col] >= start] if isinstance(col, str) else df
        if end:
            col = "date" if "date" in df.columns else df.index
            df = df[df[col] <= end] if isinstance(col, str) else df
        return df

    def get_index_daily(self, name, start=None, end=None):
        return self._index_bars.get(name, pd.DataFrame())

    def get_trading_dates(self, start="", end=""):
        return pd.DataFrame({"date": self._trading_dates}) if self._trading_dates else pd.DataFrame()

    def set_trading_dates(self, dates: list[str]):
        self._trading_dates = dates

    def get_market_trend(self, index_name="shanghai", window=20):
        df = self._index_bars.get(index_name, pd.DataFrame())
        if df.empty or "close" not in df.columns:
            return pd.DataFrame()
        df = df.sort_index().reset_index()
        df.rename(columns={df.columns[0]: "date"}, inplace=True)
        df["ma"] = df["close"].rolling(window).mean()
        df["pct"] = df["close"].pct_change() * 100
        return df

    def get_market_volume(self, index_name="shanghai", window=20):
        df = self._index_bars.get(index_name, pd.DataFrame())
        if df.empty or "volume" not in df.columns:
            return pd.DataFrame()
        df = df.sort_index().reset_index()
        df.rename(columns={df.columns[0]: "date"}, inplace=True)
        df["volume_ma"] = df["volume"].rolling(window).mean()
        df["volume_ratio"] = df["volume"] / df["volume_ma"].replace(0, float("nan"))
        return df

    def get_market_breadth(self, date):
        return self._breadth_data

    def get_northbound_net(self, start, end):
        return self._northbound_data

    def get_industry_compare(self, date):
        return self._industry_data

    def get_industry_compare_range(self, start, end):
        return self._industry_data

    def get_hot_stocks(self, date):
        return self._hot_stocks_data

    def get_fund_flow(self, symbol, start="", end=""):
        return self._fund_flow_data

    def get_fund_flow_batch(self, symbols, date):
        return self._fund_flow_data

    def get_dragon_tiger(self, date):
        return self._dragon_tiger_data

    def get_quarterly(self, symbol, report_date=""):
        return self._quarterly_data

    def get_latest_quarterly_batch(self, symbols, as_of):
        return self._quarterly_data

    def get_trading_days_before(self, date, n):
        if not self._trading_dates:
            return []
        dates = sorted(self._trading_dates)
        if date not in dates:
            return []
        idx = dates.index(date)
        return dates[max(0, idx - n + 1): idx + 1]


@pytest.fixture
def router():
    return MockDataRouter()


@pytest.fixture
def sample_daily_bars():
    """生成 200 个交易日的模拟日线数据。"""
    import numpy as np
    dates = pd.bdate_range("2024-01-01", periods=200)
    np.random.seed(42)
    base = 10.0
    prices = []
    for i in range(200):
        base *= 1 + np.random.normal(0, 0.02)
        prices.append(base)
    df = pd.DataFrame({
        "open": prices,
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.98 for p in prices],
        "close": prices,
        "volume": np.random.randint(1_000_000, 10_000_000, 200),
        "amount": np.random.uniform(1e7, 1e8, 200),
    }, index=dates)
    return df


@pytest.fixture
def sample_index_bars():
    """生成模拟指数日线。"""
    import numpy as np
    dates = pd.bdate_range("2024-01-01", periods=200)
    np.random.seed(1)
    base = 3000.0
    prices = []
    for i in range(200):
        base *= 1 + np.random.normal(0, 0.005)
        prices.append(base)
    df = pd.DataFrame({
        "close": prices,
        "volume": np.random.randint(1e8, 5e8, 200),
    }, index=dates)
    return df
