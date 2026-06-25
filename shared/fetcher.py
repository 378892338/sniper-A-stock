"""统一请求机制层 — 反检测 + 数据源自动升降级

核心设计:
  - Fetcher 不新建 ABC、不做源路由
  - 遍历数据源时先检查 HealthTracker 健康状态
  - 连续失败自动降级（冷却），恢复后自动升级
  - 委托 get_source() 获取数据源实例
"""

import random
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
}


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
                 burst_pause_max: float = 8.0):
        self.mean_delay = mean_delay
        self.std_delay = std_delay
        self.burst_limit = burst_limit
        self.burst_pause_min = burst_pause_min
        self.burst_pause_max = burst_pause_max
        self._request_count = 0
        self._last_request_time = 0.0

    def get_user_agent(self) -> str:
        return random.choice(USER_AGENTS)

    def wait(self, bypass: bool = False, failure_multiplier: float = 1.0):
        """请求前等待。bypass=True 跳过延时（用于无网络操作）。

        Args:
            bypass: 跳过延时
            failure_multiplier: 失败退避系数 — 源连续失败越多，等待越久
        """
        if bypass:
            return
        now = time.time()
        delay_mult = 1.0 if self._is_trading_hours() else 2.0

        mu = np.log(self.mean_delay**2 / np.sqrt(self.std_delay**2 + self.mean_delay**2))
        sigma = np.sqrt(np.log(1 + self.std_delay**2 / self.mean_delay**2))
        delay = float(np.random.lognormal(mu, sigma)) * delay_mult * failure_multiplier
        delay = max(0.3, min(delay, 10.0))

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

    流程:
      1. symbol 过滤（ST/920）
      2. 按优先级遍历数据源，先检查健康状态
      3. 某个源连续失败 3 次 → 自动降级到底部
      4. 成功 → Normalizer → validate_download → 返回
    """

    def __init__(self, guard: FetcherGuard | None = None):
        from data.local.warehouse import LocalDataWarehouse
        self.guard = guard or FetcherGuard()
        self.wh = LocalDataWarehouse()
        # 源级别缓存：记录本实例内各源的连续失败次数
        self._consecutive_failures: dict[str, int] = defaultdict(int)

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

        # 确定源顺序 & 按健康状态过滤
        base_order = [source_name] if source_name else list(DATA_SOURCE_PREFERENCE)
        base_order = [n for n in base_order if n in _sources]

        # 健康排序：可用源在前，不可用源在后
        healthy = []
        degraded = []
        for name in base_order:
            ep = SOURCE_ENDPOINTS.get(name)
            if ep and health_tracker.is_available(ep):
                healthy.append(name)
            else:
                degraded.append(name)

        # 按连续失败次数再细化排序
        def sort_key(n):
            return self._consecutive_failures.get(n, 0)

        healthy.sort(key=sort_key)
        degraded.sort(key=sort_key)
        source_list = healthy + degraded

        last_error = None
        used_source = None
        for src_name in source_list:
            # 跳过已标记不可用的源（除非 recovery interval 到了）
            ep = SOURCE_ENDPOINTS.get(src_name)
            if ep and not health_tracker.is_available(ep):
                # 检查是否到了恢复间隔
                if not health_tracker.should_attempt_recovery(ep):
                    logger.debug(f"{symbol}: {src_name} 健康检查未通过，跳过")
                    continue
                else:
                    health_tracker.mark_recovery_attempt(ep)

            # 按连续失败次数计算退避系数
            fail_count = self._consecutive_failures.get(src_name, 0)
            fail_mult = 1.0 + fail_count * 2.0  # 0次=1x, 1次=3x, 2次=5x, 3次=7x
            self.guard.wait(failure_multiplier=fail_mult)
            try:
                ds: DataSource = get_source(src_name)
                raw = ds.fetch_daily(symbol, start, end or "2099-12-31")
            except Exception as e:
                last_error = e
                self._consecutive_failures[src_name] += 1
                logger.debug(f"{symbol}: {src_name} 失败 ({self._consecutive_failures[src_name]}连败)")
                continue

            if raw is None or raw.empty:
                self._consecutive_failures[src_name] += 1
                logger.debug(f"{symbol}: {src_name} 空数据")
                continue

            # Normalizer
            try:
                df = normalize(raw, symbol, src_name)
            except Exception as e:
                self._consecutive_failures[src_name] += 1
                logger.debug(f"{symbol}: Normalizer 失败 ({e})")
                continue

            # 成功：重置失败计数
            self._consecutive_failures[src_name] = 0
            used_source = src_name

            # 前置校验
            try:
                from data.quality import validate_download
                problems = validate_download(df, symbol)
                if problems:
                    logger.warning(f"{symbol} 数据问题: {', '.join(problems[:2])}")
            except ImportError:
                pass
            return df

        # 所有源都失败
        if used_source is None and last_error:
            logger.warning(f"{symbol}: 所有源均失败")

        # 有效降级记录
        degraded_sources = [n for n in degraded if self._consecutive_failures.get(n, 0) >= 3]
        if degraded_sources:
            logger.debug(f"当前降级源: {degraded_sources}")

        return pd.DataFrame()

    # ── 批量 ──

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

    # ── 健康状态 ──

    def health_status(self) -> list[dict]:
        from shared.retry import health_tracker
        return [
            {"endpoint": ep, "available": False}
            for ep in health_tracker.unavailable_endpoints
        ]

    # ── 内部过滤 ──

    def _should_skip(self, symbol: str) -> bool:
        if symbol.startswith("920"):
            return True
        try:
            st_list = self.wh.get_stock_list(status="st")
            if st_list is not None and not st_list.empty:
                return symbol in st_list["symbol"].values
        except Exception:
            pass
        return False
