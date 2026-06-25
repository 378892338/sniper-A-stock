"""同花顺 (10jqka/hexin) 数据源 — 直连HTTP，作为行情备源

数据源特征:
  - 免费、无需 token
  - 日线数据完整（上市至今）
  - 单只 ~0.3s，批量可按代码分段并发
  - 提供 OHLCV + 涨跌幅

API 端点:
  (旧) http://d.10jqka.com.cn/v2/line/hq_day/{code}/last.js  — ❌ 已下线 (2025)
  (新) http://d.10jqka.com.cn/v6/line/{code}/01/all.js       — ✅ v6 紧凑格式

代码格式 (v6):
  hs_{6位数字代码} 前缀，不分板块

数据解码 (v6 紧凑编码):
  响应含 price/volumn/dates 三个紧凑数组（元素是逗号分隔字符串）。
  price → 拼接拆分后每4个一组 → [low, open_delta, high_delta, close_delta]
  实际价格 = (base + delta) / priceFactor

反爬说明:
  同花顺 WAF 对 UA、Referer、请求频率较敏感。
  已集成 AntiCrawlGuard 全面伪装。
"""

import gzip
import json
import re
import urllib.error
import urllib.request
from io import BytesIO

import pandas as pd
from data.interfaces import DataSource
from shared.retry import retry, health_tracker
from shared.anticrawl import AntiCrawlGuard
from core.logger import get_logger

logger = get_logger("data.10jqka")

# ── JSONP 包裹提取 ──
# v6 响应: quotebridge_v6_line_hs_600436_01_all({...json...})
_JSONP_RE = re.compile(r'\{.*\}', re.DOTALL)

_OUTPUT_COLUMNS = [
    "date", "open", "high", "low", "close", "volume", "amount", "pct_chg",
]


def _code_to_10jqka(symbol: str) -> str:
    """统一代码 → 同花顺 v6 格式 (hs_XXXXXX)"""
    for p in ("sh", "sz", "bj"):
        if symbol.startswith(p):
            symbol = symbol[2:]
            break
    return f"hs_{symbol}"


def _parse_compact_array(arr, dtype: type = str):
    """解析 v6 紧凑数组：可能是 list[str] 或纯逗号分隔 str → 拼接 → 拆分"""
    if isinstance(arr, str):
        arr = [arr]
    raw = ",".join(arr)  # 用逗号拼接，防边界粘连
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    if dtype == int:
        result = []
        for x in parts:
            if x == "-":  # v6 用 - 表示缺失值
                result.append(0)
            else:
                try:
                    result.append(int(x))
                except ValueError:
                    result.append(0)
        return result
    return parts


def _build_dates(start: str, sort_year: list, date_strings: list[str]) -> list[str]:
    """根据 sortYear 和 MMDD 日期串重建完整日期列表 (YYYY-MM-DD)。"""
    dates = []
    idx = 0
    for year, count in sort_year:
        for _ in range(count):
            if idx >= len(date_strings):
                break
            mmdd = date_strings[idx]
            dates.append(f"{year}-{mmdd[:2]}-{mmdd[2:]}")
            idx += 1
        if idx >= len(date_strings):
            break
    return dates


class HexinDataSource(DataSource):
    """同花顺/10jqka 直连数据源"""

    ENDPOINT = "10jqka_price"

    def __init__(self):
        self.guard = AntiCrawlGuard("10jqka")

    def name(self) -> str:
        return "10jqka"

    def is_available(self) -> bool:
        return health_tracker.is_available(self.ENDPOINT)

    @retry(max_retries=2, base_delay=2.0)
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """获取个股日线"""
        try:
            code = _code_to_10jqka(symbol)
            df = self._fetch_kline(code)
            if df.empty:
                return df

            df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
            if df.empty:
                return df

            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = symbol

            health_tracker.record_success(self.ENDPOINT)
            self.guard.on_success()
            return df.set_index("date").sort_index()
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            self.guard.on_failure()
            logger.warning(f"10jqka 获取 {symbol} 失败: {e}")
            raise

    def _fetch_kline(self, code: str) -> pd.DataFrame:
        """获取单只股票完整日线 (v6 紧凑格式)"""
        url = f"http://d.10jqka.com.cn/v6/line/{code}/01/all.js"

        self.guard.wait()

        headers = self.guard.get_headers(
            custom_referer="https://www.10jqka.com.cn/"
        )

        logger.debug(f"10jqka GET {code}")

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_bytes = resp.read()
                # 处理 gzip/deflate 压缩
                ce = resp.headers.get("Content-Encoding", "")
                if "gzip" in ce:
                    raw_bytes = gzip.decompress(raw_bytes)
                elif "deflate" in ce:
                    raw_bytes = gzip.decompress(raw_bytes)  # deflate ~ gzip
                raw = raw_bytes.decode("utf-8", errors="ignore")
                self.guard.update_cookies(dict(resp.headers))
        except urllib.error.HTTPError as e:
            if e.code == 403:
                logger.warning(f"10jqka 403 被拦截 (code={code})，轮换会话...")
                self.guard.reset()
            raise

        json_match = _JSONP_RE.search(raw)
        if not json_match:
            logger.debug(f"10jqka {code}: 未找到 JSON 数据")
            return pd.DataFrame()

        try:
            data = json.loads(json_match.group(0))
        except json.JSONDecodeError as e:
            logger.warning(f"10jqka {code}: JSON 解析失败: {e}")
            return pd.DataFrame()

        # ── 解析 v6 紧凑编码 ──
        price_factor = int(data.get("priceFactor", 100))
        total = int(data.get("total", 0))
        if total == 0:
            return pd.DataFrame()

        # 1) 价格: 每4个一组 → O/H/L/C
        price_vals = _parse_compact_array(data.get("price", []), int)
        expected = total * 4
        if len(price_vals) < expected:
            expected = (len(price_vals) // 4) * 4

        prices = []
        for i in range(0, len(price_vals) - 3, 4):
            g = price_vals[i:i + 4]
            low = g[0] / price_factor
            open_ = (g[0] + g[1]) / price_factor
            high = (g[0] + g[2]) / price_factor
            close = (g[0] + g[3]) / price_factor
            prices.append({"open": open_, "high": high, "low": low, "close": close})

        n = len(prices)
        if n == 0:
            return pd.DataFrame()

        # 2) 成交量
        vol_vals = _parse_compact_array(data.get("volumn", []), int)
        volumes = [v / 100 for v in vol_vals[:n]]

        # 3) 日期 — 用实际数据长度 trim，防 API total/sortYear 虚高
        date_strs = _parse_compact_array(data.get("dates", []), str)
        sort_year = data.get("sortYear", [])
        if len(date_strs) < n:
            n = len(date_strs)  # sync 到最小公共长度
            prices = prices[:n]
            volumes = volumes[:n]
        date_strs = date_strs[:n]
        if sort_year:
            full_dates = _build_dates(data.get("start", ""), sort_year, date_strs)
            if len(full_dates) < n:
                n = len(full_dates)
                prices = prices[:n]
                volumes = volumes[:n]
        else:
            full_dates = date_strs

        # 4) 构建 DataFrame
        rows = []
        for i in range(n):
            row = {
                "date": full_dates[i] if i < len(full_dates) else "",
                "open": prices[i]["open"],
                "high": prices[i]["high"],
                "low": prices[i]["low"],
                "close": prices[i]["close"],
                "volume": volumes[i] if i < len(volumes) else 0,
                "amount": 0.0,
                "pct_chg": 0.0,
            }
            if i > 0 and prices[i - 1]["close"] != 0:
                row["pct_chg"] = (prices[i]["close"] - prices[i - 1]["close"]) / prices[i - 1]["close"] * 100
            rows.append(row)

        df = pd.DataFrame(rows)
        return df[_OUTPUT_COLUMNS]

    @retry(max_retries=2, base_delay=1.0)
    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """获取指数日线"""
        try:
            hexin_code = _code_to_10jqka(code)
            df = self._fetch_kline(hexin_code)
            if df.empty:
                return pd.DataFrame()

            df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
            if df.empty:
                return df

            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = code

            health_tracker.record_success(self.ENDPOINT)
            self.guard.on_success()
            return df.set_index("date").sort_index()
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            self.guard.on_failure()
            logger.warning(f"10jqka 获取指数 {code} 失败: {e}")
            return pd.DataFrame()
