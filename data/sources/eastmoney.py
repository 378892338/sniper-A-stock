"""东方财富数据源 — 直连HTTP，不依赖akshare，作为主力备选"""

import json
import urllib.request

import pandas as pd

from data.interfaces import DataSource
from shared.retry import retry, health_tracker
from core.logger import get_logger

logger = get_logger("data.eastmoney")


def _em_market_code(symbol: str) -> str:
    """股票代码 → 东方财富市场代码 (0=深圳, 1=上海) + secid"""
    code = symbol
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            code = code[2:]
            break
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"1.{code}"
    return f"0.{code}"


def _em_index_code(code: str) -> str:
    """指数代码 → 东方财富 secid"""
    clean = code
    for p in ("sh", "sz", "bj"):
        if clean.startswith(p):
            clean = clean[2:]
            break
    if code.startswith("sh") or clean.startswith("000"):
        return f"1.{clean}"
    return f"0.{clean}"


class EastMoneyDataSource(DataSource):
    """东方财富直连数据源 — 免费、无需token"""

    ENDPOINT = "eastmoney_price"

    def name(self) -> str:
        return "eastmoney"

    def is_available(self) -> bool:
        return health_tracker.is_available(self.ENDPOINT)

    def _fetch_kline(self, secid: str, start: str, end: str, klt: int = 101) -> pd.DataFrame:
        """通用K线获取"""
        start_clean = start.replace("-", "")
        end_clean = end.replace("-", "")
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={secid}"
            "&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt={klt}&fqt=1"
            f"&beg={start_clean}&end={end_clean}"
            "&lmt=5000"
        )
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", "https://quote.eastmoney.com/")

        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())

        if body.get("data") is None or body["data"].get("klines") is None:
            return pd.DataFrame()

        rows = []
        for line in body["data"]["klines"]:
            parts = line.split(",")
            if len(parts) < 11:
                continue
            rows.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
                "pct_chg": float(parts[8]) if len(parts) > 8 else 0,
                "turnover": float(parts[10]) if len(parts) > 10 else 0,
            })

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()

    @retry(max_retries=2, base_delay=1.0)
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        try:
            secid = _em_market_code(symbol)
            df = self._fetch_kline(secid, start, end)
            if df.empty:
                return df
            df["symbol"] = symbol
            health_tracker.record_success(self.ENDPOINT)
            return df
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"eastmoney 获取个股 {symbol} 失败: {e}")
            raise

    @retry(max_retries=2, base_delay=1.0)
    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        try:
            secid = _em_index_code(code)
            df = self._fetch_kline(secid, start, end)
            if df.empty:
                return df
            df["symbol"] = code
            if "amount" not in df.columns:
                df["amount"] = 0.0
            health_tracker.record_success(self.ENDPOINT)
            return df
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"eastmoney 获取指数 {code} 失败: {e}")
            raise
