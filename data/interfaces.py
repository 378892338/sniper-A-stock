"""数据接口定义（抽象基类）"""

from abc import ABC, abstractmethod

import pandas as pd


class DataSource(ABC):
    """数据源抽象基类 — 定义统一接口"""

    @abstractmethod
    def name(self) -> str:
        """数据源名称"""

    @abstractmethod
    def is_available(self) -> bool:
        """当前是否可用"""

    @abstractmethod
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """获取个股日线"""

    @abstractmethod
    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """获取指数日线"""

    def fetch_daily_batch(self, symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        """批量获取个股日线（默认逐条获取，子类可覆盖优化）"""
        result = {}
        for sym in symbols:
            df = self.fetch_daily(sym, start, end)
            if not df.empty:
                result[sym] = df
        return result

    def fetch_industry_members(self) -> pd.DataFrame:
        """获取行业分类数据（默认空，子类覆盖）"""
        return pd.DataFrame()

    def fetch_concept_members(self) -> pd.DataFrame:
        """获取概念板块数据（默认空，子类覆盖）"""
        return pd.DataFrame()


class FundFlowSource(ABC):
    """资金流数据源抽象基类"""

    @abstractmethod
    def name(self) -> str:
        """数据源名称"""

    @abstractmethod
    def is_available(self) -> bool:
        """是否可用"""

    @abstractmethod
    def fetch_northbound_flow(self, start: str, end: str) -> pd.DataFrame | None:
        """获取北向资金净流入"""

    @abstractmethod
    def fetch_market_turnover(self, code: str, start: str, end: str) -> pd.DataFrame | None:
        """获取市场成交额趋势"""
