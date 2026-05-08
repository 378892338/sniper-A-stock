"""tushare 数据源实现

免费 token 可用接口:
- moneyflow_hsgt: 北向资金
- stock_basic: 股票基础信息
- hs_const: 沪深港通成分股

不可用（需更高权限）: daily, index_daily, pro_bar 等行情接口
"""

import pandas as pd

from data.interfaces import DataSource, FundFlowSource
from shared.retry import retry, health_tracker
from core.logger import get_logger

logger = get_logger("data.tushare")


def _get_pro():
    """延迟初始化 tushare pro API"""
    from config.settings import TUSHARE_TOKEN
    if not TUSHARE_TOKEN:
        return None
    import tushare as ts
    return ts.pro_api(TUSHARE_TOKEN)


class TushareDataSource(DataSource):
    """基于 tushare 的行情数据源

    权限自动检测: 首次调用时尝试获取数据，若 token 无 daily/index_daily 权限
    则标记不可用，后续自动降级到下一数据源。
    """

    _HAS_PRICE_PERMISSION: bool | None = None   # None=未检测, True=可用, False=不可用
    ENDPOINT = "tushare_price"

    def name(self) -> str:
        return "tushare"

    def is_available(self) -> bool:
        pro = _get_pro()
        if pro is None:
            return False
        if self._HAS_PRICE_PERMISSION is False:
            return False
        return health_tracker.is_available(self.ENDPOINT)

    @retry(max_retries=3, base_delay=1.0)
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        pro = _get_pro()
        if pro is None:
            return pd.DataFrame()

        ts_code = self._to_ts_code(symbol)
        try:
            df = pro.daily(
                ts_code=ts_code,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
            if TushareDataSource._HAS_PRICE_PERMISSION is None:
                TushareDataSource._HAS_PRICE_PERMISSION = True
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.rename(columns={"trade_date": "date", "vol": "volume"})
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = symbol
            df = df.sort_values("date")
            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date")
        except Exception as e:
            err_msg = str(e)
            _mark_permission_denied(err_msg)
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"tushare 获取个股 {symbol} 失败: {e}")
            return pd.DataFrame()

    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        pro = _get_pro()
        if pro is None:
            return pd.DataFrame()

        ts_code = self._to_index_ts_code(code)
        try:
            df = pro.index_daily(
                ts_code=ts_code,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
            if TushareDataSource._HAS_PRICE_PERMISSION is None:
                TushareDataSource._HAS_PRICE_PERMISSION = True
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.rename(columns={"trade_date": "date", "vol": "volume"})
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = code
            df = df.sort_values("date")
            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date")
        except Exception as e:
            err_msg = str(e)
            _mark_permission_denied(err_msg)
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"tushare 获取指数 {code} 失败: {e}")
            return pd.DataFrame()

    @staticmethod
    def _to_ts_code(symbol: str) -> str:
        if symbol.startswith(("60", "68")):
            return f"{symbol}.SH"
        return f"{symbol}.SZ"

    @staticmethod
    def _to_index_ts_code(code: str) -> str:
        if code.startswith("000"):
            return f"{code}.SH"
        return f"{code}.SZ"


class TushareFundFlowSource(FundFlowSource):
    """基于 tushare 的资金流数据源

    北向资金走 moneyflow_hsgt（免费可用），
    成交额走 index_daily（免费不可用，fallback 到 akshare）。
    """

    ENDPOINT = "tushare_fund_flow"

    def name(self) -> str:
        return "tushare_fund_flow"

    def is_available(self) -> bool:
        """北向资金走 moneyflow_hsgt，免费 token 即可用"""
        return _get_pro() is not None and health_tracker.is_available(self.ENDPOINT)

    def fetch_northbound_flow(self, start: str, end: str) -> pd.DataFrame | None:
        pro = _get_pro()
        if pro is None:
            return None

        try:
            df = pro.moneyflow_hsgt(
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
            if df is None or df.empty:
                return None

            df = df.rename(columns={
                "trade_date": "date",
                "north_money": "net_flow",
            })
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date")
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"tushare 获取北向资金失败: {e}")
            return None

    def fetch_market_turnover(self, code: str, start: str, end: str) -> pd.DataFrame | None:
        # 成交额走 index_daily，需付费 token。无权限时返回 None 让上层降级
        if TushareDataSource._HAS_PRICE_PERMISSION is False:
            return None

        pro = _get_pro()
        if pro is None:
            return None

        ts_code = TushareDataSource._to_index_ts_code(code)
        try:
            df = pro.index_daily(
                ts_code=ts_code,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
            if df is None or df.empty:
                return None
            df = df.rename(columns={"trade_date": "date"})
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date")
        except Exception as e:
            err_msg = str(e)
            _mark_permission_denied(err_msg)
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"tushare 获取成交额数据 {code} 失败: {e}")
            return None


def _mark_permission_denied(err_msg: str):
    """检测是否为权限不足错误，若是则标记所有 price 接口不可用"""
    keywords = ["权限", "permission", "接口权限", "此接口", "无权限", "no permission"]
    if any(kw in err_msg.lower() for kw in keywords):
        TushareDataSource._HAS_PRICE_PERMISSION = False
        logger.info("tushare 行情接口权限不足，将自动降级到下一数据源")
