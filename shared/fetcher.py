"""统一请求机制层 — 反检测 + 数据源自动升降级

核心设计:
  - Fetcher 不新建 ABC、不做源路由
  - 遍历数据源时先检查 HealthTracker 健康状态
  - 连续失败自动降级（冷却），恢复后自动升级
  - 委托 get_source() 获取数据源实例
"""

import random
import threading
import time
from collections import defaultdict

import numpy as np
import pandas as pd

from core.logger import get_logger

logger = get_logger("shared.fetcher")

# ── 20+ 主流浏览器 UA 池 ──
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edge/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edge/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 OPR/110.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Android 14; Mobile; rv:126.0) Gecko/126.0 Firefox/126.0",
]

# 数据源名 → HealthTracker endpoint 映射
SOURCE_ENDPOINTS = {
    "eastmoney": "eastmoney_price",
    "sina": "sina_price",
    "akshare": "akshare_price",
    "akshare_daily": "akshare_daily_price",
    "tushare": "tushare_price",
    "baostock": "baostock_price",
    "jqdata": "jqdata_price",
    "mootdx": "mootdx_price",
    "10jqka": "10jqka_price",
}

# 通用超时配置（Fix K: 可配置，默认 15s）
FETCH_TIMEOUT = 15  # per-symbol 超时
_ASSERT_LOCK = threading.Lock()


class FetcherGuard:
    """请求伪装层 — 模拟真人浏览器行为。

    功能:
      - User-Agent 轮换: 每次请求随机取
      - 请求间隔: 对数正态分布，均值 1.5s
      - 突发控制: 连续 N 次后强制停顿
      - 时段感知: 非交易时段延时 ×2
    """

    def __init__(self, mean_delay: float = 1.5, std_delay: float = 0.5,
                 burst_limit: int = 5, burst_pause_min: float = 3.0,
                 burst_pause_max: float = 8.0,
                 timeout: int = 15):
        self.mean_delay = mean_delay
        self.std_delay = std_delay
        self.burst_limit = burst_limit
        self.burst_pause_min = burst_pause_min
        self.burst_pause_max = burst_pause_max
        self.timeout = timeout
        self._request_count = 0
        self._last_request_time = 0.0
        self._lock = _ASSERT_LOCK  # Fix G: _request_count 的线程保护

    def get_user_agent(self) -> str:
        return random.choice(USER_AGENTS)

    def wait(self, bypass: bool = False, failure_multiplier: float = 1.0):
        if bypass:
            return
        now = time.time()
        delay_mult = 1.0 if self._is_trading_hours() else 2.0

        mu = np.log(self.mean_delay**2 / np.sqrt(self.std_delay**2 + self.mean_delay**2))
        sigma = np.sqrt(np.log(1 + self.std_delay**2 / self.mean_delay**2))
        delay = float(np.random.lognormal(mu, sigma)) * delay_mult * failure_multiplier
        delay = max(0.3, min(delay, 10.0))

        with self._lock:  # Fix G
            self._request_count += 1
            if self._request_count >= self.burst_limit:
                delay += random.uniform(self.burst_pause_min, self.burst_pause_max)
                self._request_count = 0

        elapsed = now - self._last_request_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_time = time.time()

    def _is_trading_hours(self) -> bool:
        now = time.localtime()
        h, m = now.tm_hour, now.tm_min
        return (h == 9 and m >= 30) or (10 <= h <= 14)


class Fetcher:
    """统一请求入口 — 反检测 + 数据源自动升降级。

    Fix F: ST 列表缓存 300s TTL
    Fix G: _consecutive_failures 全路径 Lock 保护
    Fix K: per-symbol 15s 超时包装
    Fix Q: _consecutive_failures 时间衰减（>1800s 减半）
    """

    def __init__(self, guard: FetcherGuard | None = None):
        from data.local.warehouse import LocalDataWarehouse
        self.guard = guard or FetcherGuard()
        self.wh = LocalDataWarehouse()
        self._consecutive_failures: dict[str, int] = defaultdict(int)
        self._failure_timestamps: dict[str, float] = {}
        self._lock = _ASSERT_LOCK
        # Fix F: ST 列表缓存
        self._st_set: set | None = None
        self._st_cache_time: float = 0.0
        self._st_cache_ttl = 300  # 300s

    def _get_st_set(self) -> set:
        """Fix F: 延迟加载 + 300s 缓存"""
        now = time.monotonic()
        if self._st_set is not None and (now - self._st_cache_time) < self._st_cache_ttl:
            return self._st_set
        try:
            st_list = self.wh.get_stock_list(status="st")
            self._st_set = set(st_list["symbol"].values) if st_list is not None and not st_list.empty else set()
            self._st_cache_time = now
        except Exception:
            self._st_set = self._st_set or set()
        return self._st_set

    def _apply_time_decay(self, src_name: str):
        """Fix Q: 失败计数时间衰减 — >1800s 未更新则减半"""
        ts = self._failure_timestamps.get(src_name)
        now = time.time()
        if ts and (now - ts) > 1800:
            old = self._consecutive_failures.get(src_name, 0)
            if old > 0:
                self._consecutive_failures[src_name] = max(1, old // 2)
                logger.debug(f"  {src_name} 失败计数衰减: {old} → {self._consecutive_failures[src_name]}")
        self._failure_timestamps[src_name] = now

    def _incr_fail(self, src_name: str):
        """Fix G: 线程安全的失败计数递增 + Q: 时间戳记录"""
        with self._lock:
            self._apply_time_decay(src_name)
            self._consecutive_failures[src_name] += 1
            self._failure_timestamps[src_name] = time.time()

    def _reset_fail(self, src_name: str):
        """Fix G: 线程安全的失败计数重置"""
        with self._lock:
            self._consecutive_failures[src_name] = 0
            self._failure_timestamps.pop(src_name, None)

    def _get_fail_count(self, src_name: str) -> int:
        """Fix G: 线程安全的读取"""
        with self._lock:
            self._apply_time_decay(src_name)
            return self._consecutive_failures.get(src_name, 0)

    def fetch_stock_daily(self, symbol: str, start: str = "2000-01-01",
                          end: str | None = None,
                          source_name: str | None = None) -> pd.DataFrame:
        if self._should_skip(symbol):
            return pd.DataFrame()

        from data.sources import get_source, _sources
        from data.normalizer import normalize
        from data.interfaces import DataSource
        from config.settings import DATA_SOURCE_PREFERENCE
        from shared.retry import health_tracker

        base_order = [source_name] if source_name else list(DATA_SOURCE_PREFERENCE)
        base_order = [n for n in base_order if n in _sources]

        healthy = []
        degraded = []
        for name in base_order:
            ep = SOURCE_ENDPOINTS.get(name)
            if ep and health_tracker.is_available(ep):
                healthy.append(name)
            else:
                degraded.append(name)

        def sort_key(n):
            return self._get_fail_count(n)

        healthy.sort(key=sort_key)
        degraded.sort(key=sort_key)
        source_list = healthy + degraded

        last_error = None
        used_source = None
        tried_sources = []
        for src_name in source_list:
            ep = SOURCE_ENDPOINTS.get(src_name)
            if ep and not health_tracker.is_available(ep):
                if not health_tracker.should_attempt_recovery(ep):
                    logger.debug(f"{symbol}: {src_name} 健康检查未通过，跳过")
                    continue
                else:
                    health_tracker.mark_recovery_attempt(ep)

            fail_count = self._get_fail_count(src_name)
            fail_mult = 1.0 + fail_count * 2.0
            self.guard.wait(failure_multiplier=fail_mult)
            tried_sources.append(src_name)
            try:
                ds: DataSource = get_source(src_name)
                # Fix K: per-symbol 超时包装
                from concurrent.futures import ThreadPoolExecutor, TimeoutError
                _pool = ThreadPoolExecutor(max_workers=1)
                _ft = _pool.submit(ds.fetch_daily, symbol, start, end or "2099-12-31")
                try:
                    raw = _ft.result(timeout=self.guard.timeout)
                except TimeoutError:
                    _ft.cancel()
                    self._incr_fail(src_name)
                    logger.debug(f"{symbol}: {src_name} 超时 ({self.guard.timeout}s)")
                    _pool.shutdown(wait=False)
                    continue
                finally:
                    _pool.shutdown(wait=False)
            except Exception as e:
                last_error = e
                self._incr_fail(src_name)
                logger.debug(f"{symbol}: {src_name} 失败 ({self._get_fail_count(src_name)}连败): {e}")
                continue

            if raw is None or raw.empty:
                self._incr_fail(src_name)
                logger.debug(f"{symbol}: {src_name} 空数据")
                continue

            try:
                df = normalize(raw, symbol, src_name)
            except Exception as e:
                self._incr_fail(src_name)
                logger.debug(f"{symbol}: Normalizer 失败 ({e})")
                continue

            self._reset_fail(src_name)
            used_source = src_name

            try:
                from data.quality import validate_download
                problems = validate_download(df, symbol)
                if problems:
                    logger.warning(f"{symbol} 数据问题: {', '.join(problems[:2])}")
            except ImportError:
                pass
            return df

        # Fix H: 所有源失败时聚合 WARNING
        if used_source is None:
            logger.warning(f"{symbol}: 尝试 {len(tried_sources)} 个源均失败: {', '.join(tried_sources[:5])}")

        return pd.DataFrame()

    def fetch_stock_list(self, symbols: list[str], start: str = "2000-01-01",
                         end: str | None = None,
                         source_name: str | None = None) -> dict[str, pd.DataFrame]:
        results = {}
        for sym in symbols:
            df = self.fetch_stock_daily(sym, start, end, source_name)
            if not df.empty:
                results[sym] = df
        logger.info(f"批量获取: {len(results)}/{len(symbols)} 成功")
        return results

    def health_status(self) -> list[dict]:
        from shared.retry import health_tracker
        return [
            {"endpoint": ep, "available": False}
            for ep in health_tracker.unavailable_endpoints
        ]

    def _should_skip(self, symbol: str) -> bool:
        if symbol.startswith("920"):
            return True
        try:
            st_set = self._get_st_set()  # Fix F: 使用缓存
            return symbol in st_set
        except Exception:
            pass
        return False
