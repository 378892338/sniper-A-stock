"""东财资金流直连 — push2 接口。

当前状态：_disabled=True（东财 API 持续阻断，恢复后激活）
因子使用字段：main_net (L2 权重 7%), super_large (L2 权重 5%)
"""

import pandas as pd

from data.interfaces import DataSource
from core.logger import get_logger

logger = get_logger("data.eastmoney_fundflow")

_disabled = True  # 东财 API 阻断中


class EastMoneyFundFlowSource(DataSource):
    """东财资金流直连源（disabled：API 恢复后激活）"""

    ENDPOINT = "eastmoney_fundflow"

    def name(self) -> str:
        return "eastmoney_fundflow"

    def is_available(self) -> bool:
        return False  # 东财 API 阻断，暂不可用

    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame()
