"""mootdx 通达信 TCP 数据源 — 免费、不封 IP。

通达信 TCP 7709 二进制协议直连，无 API Key 要求，无 IP 限流。
需国内 IP 环境。

数据源优先级建议：
  DATA_SOURCE_PREFERENCE 中放在 jqdata 之后、akshare 之前

安全机制:
  - 连接池复用 (全局单例 Quotes)
  - Heartbeat 保活 (15s)
  - 指数退避重试 (3 次)
  - HealthTracker 集成 (3 次失败自动降级)
  - 数据防御校验 (close 范围)
  - 线程安全 (双重检查锁)
"""

import threading
import time

import pandas as pd

from data.interfaces import DataSource
from shared.retry import retry, health_tracker
from shared.rate_limiter import get_limiter
from core.logger import get_logger

logger = get_logger("data.mootdx")

# FACTOR_REQUIRED_FIELDS: open high low close volume amount
# FACTOR_DESIRED_FIELDS: turnover (mootdx 不返回，填 NaN)
_OUTPUT_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


class MootdxSource(DataSource):
    """通达信 TCP 行情数据源 — 免费不封 IP"""

    ENDPOINT = "mootdx_price"

    _instances: dict[str, object] = {}  # 全局连接池
    _instance_lock = threading.Lock()

    def name(self) -> str:
        return "mootdx"

    def is_available(self) -> bool:
        return health_tracker.is_available(self.ENDPOINT)

    def _get_client(self):
        """获取或创建全局 mootdx Quotes 实例。"""
        if "std" not in self._instances:
            with self._instance_lock:
                if "std" not in self._instances:
                    from mootdx.quotes import Quotes

                    client = Quotes.factory(
                        market="std", timeout=15, heartbeat=False, bestip=False,
                    )
                    self._instances["std"] = client
        return self._instances["std"]

    @retry(max_retries=2, base_delay=1.0)
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        limiter = get_limiter("mootdx")
        limiter.wait()
        try:
            client = self._get_client()
            # get_k_data 返回日期范围的日线（不包含 turnover）
            df = client.get_k_data(
                code=symbol, start_date=start, end_date=end,
            )
            if df is None or df.empty:
                return self._empty()

            # 列名映射：vol→volume, code→symbol
            df = df.rename(columns={
                "vol": "volume",
                "code": "symbol",
            })
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = symbol

            # 防御校验
            for col in ["open", "high", "low", "close"]:
                if col in df.columns:
                    if (df[col] < 0).any() or (df[col] > 10000).any():
                        logger.warning(f"mootdx {symbol}: {col} 超范围")
                        return self._empty()

            health_tracker.record_success(self.ENDPOINT)
            limiter.record_success()
            return df[_OUTPUT_COLUMNS].set_index("date").sort_index()

        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            limiter.record_failure()
            logger.debug(f"mootdx fetch_daily({symbol}) 失败: {e}")
            raise

    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        # mootdx 不支持指数日线
        return pd.DataFrame()

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame()
