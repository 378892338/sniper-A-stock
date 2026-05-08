"""baostock 数据源实现 — 免费备选（无需token）

baostock 免费接口:
- 个股日线: query_history_k_data_plus
- 指数日线: query_history_k_data_plus (code=sh.000001)
- 不支持: 北向资金、大单、融资融券

作为降级备选，只在 akshare 核心接口不可用时启用。
"""

import pandas as pd
import numpy as np

from data.interfaces import DataSource
from shared.retry import retry, health_tracker
from core.logger import get_logger

logger = get_logger("data.baostock")

_BAOSTOCK_AVAILABLE = None  # 延迟检测


def _check_baostock():
    """检测 baostock 是否可用"""
    global _BAOSTOCK_AVAILABLE
    if _BAOSTOCK_AVAILABLE is None:
        try:
            import baostock as bs
            lg = bs.login()
            if lg.error_code == "0":
                _BAOSTOCK_AVAILABLE = True
                bs.logout()
            else:
                _BAOSTOCK_AVAILABLE = False
                logger.warning(f"baostock 登录失败: {lg.error_msg}")
        except ImportError:
            _BAOSTOCK_AVAILABLE = False
            logger.info("baostock 未安装")
    return _BAOSTOCK_AVAILABLE


def _format_baostock_code(symbol: str) -> str:
    """股票代码转 baostock 格式: sh.600000 / sz.000001"""
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"sh.{symbol}"
    elif symbol.startswith("0") or symbol.startswith("3"):
        return f"sz.{symbol}"
    elif symbol.startswith("4") or symbol.startswith("8"):
        return f"bj.{symbol}"
    return f"sz.{symbol}"


def _format_baostock_index_code(code: str) -> str:
    """指数代码转 baostock 格式，兼容带/不带交易所前缀"""
    # 去掉已有的 sh/sz/bj 前缀
    clean = code
    if code.startswith("sh"):
        clean = code[2:]
    elif code.startswith("sz"):
        clean = code[2:]
    elif code.startswith("bj"):
        clean = code[2:]
    if clean.startswith("000") or clean.startswith("000"):
        return f"sh.{clean}"
    elif clean.startswith("399"):
        return f"sz.{clean}"
    return f"sh.{clean}"


class BaostockDataSource(DataSource):
    """基于 baostock 的行情数据源（免费备选）"""

    ENDPOINT = "baostock_price"

    def name(self) -> str:
        return "baostock"

    def is_available(self) -> bool:
        return _check_baostock()

    @retry(max_retries=2, base_delay=2.0)
    def _fetch_k_data(self, bs_code: str, fields: str, start: str, end: str,
                      columns: list, symbol_val: str) -> pd.DataFrame:
        """获取K线数据的公共方法"""
        if not _check_baostock():
            return pd.DataFrame()

        import baostock as bs

        try:
            lg = bs.login()
            if lg.error_code != "0":
                health_tracker.record_failure(self.ENDPOINT)
                return pd.DataFrame()

            rs = bs.query_history_k_data_plus(
                bs_code, fields,
                start_date=start, end_date=end,
                frequency="d", adjustflag="2",
            )

            rows = []
            while (rs.error_code == "0") and rs.next():
                rows.append(rs.get_row_data())

            bs.logout()

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=columns)
            numeric_cols = [c for c in columns if c != "date"]
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = symbol_val
            df = df.dropna(subset=["close"])

            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date")

        except (ImportError, ConnectionError, RuntimeError) as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"baostock 获取 {bs_code} 失败: {e}")
            try:
                bs.logout()
            except Exception as e:
                logger.debug(f"baostock logout 失败: {e}")
            return pd.DataFrame()

    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        bs_code = _format_baostock_code(symbol)
        fields = "date,open,high,low,close,volume,amount,turn,pctChg"
        columns = [
            "date", "open", "high", "low", "close",
            "volume", "amount", "turn", "pct_chg",
        ]
        return self._fetch_k_data(bs_code, fields, start, end, columns, symbol)

    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        bs_code = _format_baostock_index_code(code)
        fields = "date,open,high,low,close,volume,amount"
        columns = ["date", "open", "high", "low", "close", "volume", "amount"]
        return self._fetch_k_data(bs_code, fields, start, end, columns, code)
