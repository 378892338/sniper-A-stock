"""东财龙虎榜直连 — datacenter 接口。

当前状态：_disabled=True（东财 API 持续阻断，恢复后激活）
因子使用字段：net_buy (L2 权重 3%)
席位 TOP5 数据因子不使用，仅原始 API 响应中提取展示。
"""

import pandas as pd

from data.interfaces import DataSource
from core.logger import get_logger

logger = get_logger("data.eastmoney_dt")

_disabled = True


class EastMoneyDTSource(DataSource):
    """东财龙虎榜直连源（disabled：API 恢复后激活）"""

    ENDPOINT = "eastmoney_dt"

    def name(self) -> str:
        return "eastmoney_dt"

    def is_available(self) -> bool:
        return False

    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame()
