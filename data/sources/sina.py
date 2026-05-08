"""新浪财经数据源 — 免费、无需token、HTTP直接调用"""

import re
import json
import time

import pandas as pd
import numpy as np

from data.interfaces import DataSource
from shared.retry import retry, health_tracker
from core.logger import get_logger

logger = get_logger("data.sina")

# K线周期映射
PERIOD_MAP = {"daily": 240, "weekly": 5, "monthly": 6}


def _symbol_to_sina(symbol: str) -> str:
    """股票代码转新浪格式: sz000001 / sh600000"""
    if symbol.startswith("sh") or symbol.startswith("sz") or symbol.startswith("bj"):
        return symbol
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"sh{symbol}"
    if symbol.startswith("0") or symbol.startswith("3"):
        return f"sz{symbol}"
    if symbol.startswith("4") or symbol.startswith("8"):
        return f"bj{symbol}"
    return f"sz{symbol}"


def _index_code_to_sina(code: str) -> str:
    """指数代码转新浪格式"""
    clean = code
    for prefix in ("sh", "sz", "bj"):
        if clean.startswith(prefix):
            clean = clean[2:]
            break
    if code.startswith("sh") or clean.startswith("000"):
        return f"sh{clean}"
    return f"sz{clean}"


class SinaDataSource(DataSource):
    """新浪财经行情数据源 — 免费备选"""

    ENDPOINT = "sina_price"

    def name(self) -> str:
        return "sina"

    def is_available(self) -> bool:
        return health_tracker.is_available(self.ENDPOINT)

    def _request_jsonp(self, url: str, params: dict) -> dict | list:
        """请求新浪 JSONP 接口并解析"""
        import urllib.request
        import urllib.parse

        query = urllib.parse.urlencode(params)
        full_url = f"{url}?{query}"
        req = urllib.request.Request(full_url)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", "https://finance.sina.com.cn/")

        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("gbk", errors="replace")

        # JSONP → JSON (去除回调函数包装)
        json_match = re.search(r"\((.*)\)", text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        return json.loads(text)

    @retry(max_retries=2, base_delay=1.0)
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        sina_sym = _symbol_to_sina(symbol)
        try:
            data = self._request_jsonp(
                "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                {"symbol": sina_sym, "scale": 240, "ma": "no", "datalen": 2000},
            )
            if not data or not isinstance(data, list):
                return pd.DataFrame()

            df = pd.DataFrame(data)
            df = df.rename(columns={
                "day": "date", "open": "open", "high": "high",
                "low": "low", "close": "close", "volume": "volume",
            })
            df["date"] = pd.to_datetime(df["date"])
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            mask = (df["date"] >= start) & (df["date"] <= end)
            df = df[mask].dropna(subset=["close"])
            df["symbol"] = symbol

            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date") if not df.empty else pd.DataFrame()
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"sina 获取个股 {symbol} 失败: {e}")
            raise

    @retry(max_retries=2, base_delay=1.0)
    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        sina_code = _index_code_to_sina(code)
        try:
            data = self._request_jsonp(
                "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                {"symbol": sina_code, "scale": 240, "ma": "no", "datalen": 2000},
            )
            if not data or not isinstance(data, list):
                return pd.DataFrame()

            df = pd.DataFrame(data)
            df = df.rename(columns={
                "day": "date", "open": "open", "high": "high",
                "low": "low", "close": "close", "volume": "volume",
            })
            df["date"] = pd.to_datetime(df["date"])
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            mask = (df["date"] >= start) & (df["date"] <= end)
            df = df[mask].dropna(subset=["close"])
            df["symbol"] = code
            # 新浪指数K线可能没有 amount 列
            if "amount" not in df.columns:
                df["amount"] = 0.0

            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date") if not df.empty else pd.DataFrame()
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"sina 获取指数 {code} 失败: {e}")
            raise
