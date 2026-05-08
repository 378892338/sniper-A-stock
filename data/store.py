"""统一数据存储 — 所有计算从这里取数

日线是唯一的事实来源，周线/月线由统一规则 resample 生成并缓存。
所有 Gate 层和回测引擎通过此模块取数，确保口径一致。

规则:
  weekly:  "W-FRI", closed="right", label="right
  monthly: "ME",     closed="right", label="right
"""

from pathlib import Path

import pandas as pd

from core.logger import get_logger

logger = get_logger("data.store")

_WEEKLY_RULE = "W-FRI"
_MONTHLY_RULE = "ME"
_RESAMPLE_KWARGS = {"closed": "right", "label": "right"}

_AGG = {
    "open": "first",
    "close": "last",
    "high": "max",
    "low": "min",
    "volume": "sum",
}


class DataStore:
    """统一数据存储

    日线是唯一事实来源，周线/月线由 resample 生成并缓存。
    """

    def __init__(self):
        self._daily: dict[str, pd.DataFrame] = {}
        self._weekly: dict[str, pd.DataFrame] = {}
        self._monthly: dict[str, pd.DataFrame] = {}

    # ── 注册 ──

    def add_daily(self, name: str, df: pd.DataFrame) -> None:
        """注册日线数据（覆盖同名）"""
        if df.empty:
            return
        self._daily[name] = df.sort_index()
        # 清除旧缓存
        self._weekly.pop(name, None)
        self._monthly.pop(name, None)

    def add_many_daily(self, data: dict[str, pd.DataFrame]) -> None:
        for name, df in data.items():
            self.add_daily(name, df)

    # ── 读取 ──

    def get_daily(self, name: str, start=None, end=None) -> pd.DataFrame | None:
        df = self._daily.get(name)
        if df is None or df.empty:
            return None
        return _slice(df, start, end)

    def get_weekly(self, name: str, start=None, end=None) -> pd.DataFrame | None:
        if name not in self._weekly:
            daily = self._daily.get(name)
            if daily is None or daily.empty:
                return None
            self._weekly[name] = _resample_weekly(daily)
        return _slice(self._weekly[name], start, end)

    def get_monthly(self, name: str, start=None, end=None) -> pd.DataFrame | None:
        if name not in self._monthly:
            daily = self._daily.get(name)
            if daily is None or daily.empty:
                return None
            self._monthly[name] = _resample_monthly(daily)
        return _slice(self._monthly[name], start, end)

    # ── 批量 ──

    def get_many_weekly(self, names: list[str]) -> dict[str, pd.DataFrame]:
        return {n: self.get_weekly(n) for n in names}

    def get_many_monthly(self, names: list[str]) -> dict[str, pd.DataFrame]:
        return {n: self.get_monthly(n) for n in names}

    # ── 元信息 ──

    @property
    def names(self) -> list[str]:
        return list(self._daily.keys())

    @property
    def stock_names(self) -> list[str]:
        """个股名称（6位数字代码）"""
        return [n for n in self._daily if n.isdigit() and len(n) == 6]

    @property
    def index_names(self) -> list[str]:
        """指数名称（非6位数字代码）"""
        return [n for n in self._daily if not (n.isdigit() and len(n) == 6)]

    def __len__(self):
        return len(self._daily)

    def __repr__(self):
        n_stocks = len(self.stock_names)
        n_indices = len(self) - n_stocks
        return f"DataStore({n_indices} indices, {n_stocks} stocks)"

    # ── 从 parquet 缓存目录加载 ──

    @classmethod
    def from_parquet_cache(cls, cache_dir: str | Path, n_stocks: int = 0) -> "DataStore":
        """从 backtest 缓存目录加载所有日线数据并构建 DataStore"""
        cache_dir = Path(cache_dir)
        store = cls()

        # 市场指数日线（仅加载新格式 market_daily_*，避免旧格式 market_*_daily 覆盖最新数据）
        for p in cache_dir.glob("market_daily_*.parquet"):
            name = p.stem.replace("market_daily_", "")
            df = pd.read_parquet(p)
            if not df.empty:
                store.add_daily(name, df)

        # ETF 日线
        for p in cache_dir.glob("etf_daily_*.parquet"):
            name = p.stem.replace("etf_daily_", "")
            df = pd.read_parquet(p)
            if not df.empty:
                store.add_daily(name, df)

        # 个股日线（n_stocks=0 表示全部）
        stock_files = sorted(cache_dir.glob("stock_*_daily.parquet"))
        if n_stocks > 0:
            stock_files = stock_files[:n_stocks]

        for f in stock_files:
            try:
                df = pd.read_parquet(f)
                if len(df) >= 200:
                    if "symbol" in df.columns:
                        sym = str(df["symbol"].iloc[0])
                    else:
                        sym = f.stem.replace("stock_", "").replace("_daily", "")
                    store.add_daily(sym, df)
            except Exception:
                continue

        logger.info(f"DataStore 从缓存加载: {len(store)} 条日线")
        return store

    def to_parquet_cache(self, cache_dir: str | Path) -> None:
        """将当前日线数据写入 parquet 缓存目录"""
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        for name, df in self._daily.items():
            if name in _MARKET_NAMES:
                df.to_parquet(cache_dir / f"market_daily_{name}.parquet")
            elif name in _ETF_NAMES:
                df.to_parquet(cache_dir / f"etf_daily_{name}.parquet")
            else:
                df.to_parquet(cache_dir / f"stock_{name}_daily.parquet")
        logger.info(f"DataStore 写入缓存: {cache_dir}")


# ── 内部工具 ──

def _resample_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    return daily.resample(_WEEKLY_RULE, **_RESAMPLE_KWARGS).agg(_AGG).dropna()


def _resample_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    return daily.resample(_MONTHLY_RULE, **_RESAMPLE_KWARGS).agg(_AGG).dropna()


def _slice(df: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    if start is None and end is None:
        return df
    if start is not None:
        df = df.loc[df.index >= start]
    if end is not None:
        df = df.loc[df.index <= end]
    return df


def _extract_name(p: Path, prefix: str | None) -> str:
    if prefix:
        return p.stem.replace(prefix, "")
    return p.stem.replace("market_", "").replace("_daily", "")


_MARKET_NAMES = {"shanghai", "shenzhen", "chinext", "csi300"}
_ETF_NAMES = {"证券", "银行", "军工", "芯片", "新能源车", "光伏",
              "消费", "医药", "酒", "科技", "有色", "煤炭", "汽车", "半导体"}
