"""源级独立限流器 — Token Bucket per source

每个数据源独立的请求间隔控制，替代 FetcherGuard 的全局延迟。
连续失败时速率自动减半，恢复后渐增至初始值。
"""

import threading
import time


class RateLimiter:
    """每源独立限流器。

    用法:
        limiter = RateLimiter(min_interval=1.5)
        limiter.wait()              # 阻塞等待直到可发起请求

    Args:
        min_interval: 最小请求间隔（秒）
        fail_backoff: 连续失败后速率减半的阈值（次数）
    """

    def __init__(self, min_interval: float = 1.5, fail_backoff: int = 3):
        self._min_interval = min_interval
        self._current_interval = min_interval
        self._fail_backoff = fail_backoff
        self._consecutive_failures = 0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        """等待直到可发起请求。阻塞当前线程。"""
        with self._lock:
            elapsed = time.time() - self._last
            if elapsed < self._current_interval:
                time.sleep(self._current_interval - elapsed)
            self._last = time.time()

    def record_success(self):
        """请求成功后调用，渐增至初始速率。"""
        with self._lock:
            self._consecutive_failures = 0
            self._current_interval = max(
                self._current_interval * 0.9, self._min_interval
            )

    def record_failure(self):
        """请求失败后调用，速率减半。"""
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._fail_backoff:
                self._current_interval = min(
                    self._current_interval * 2.0, 30.0
                )

    @property
    def interval(self) -> float:
        return self._current_interval


# 全局限流器实例（每个源独立）
_instances: dict[str, RateLimiter] = {}
_instances_lock = threading.Lock()


def get_limiter(source_name: str) -> RateLimiter:
    """获取或创建指定源的限流器。

    每源默认间隔：
      mootdx:    0.3s (TCP, 低封禁风险)
      eastmoney: 1.5s (HTTP, 高封禁风险)
      sina:      0.5s (HTTP, 低风险)
      tencent:   0.2s (HTTP, 极低风险)
      akshare:   0.5s (HTTP)
      jqdata:    0.0s (SDK, 官方认证)
      default:   1.0s
    """
    DEFAULT_INTERVALS = {
        "mootdx": 0.3,
        "akshare": 0.5,
        "eastmoney": 1.5,
        "eastmoney_fundflow": 1.5,
        "eastmoney_dt": 1.5,
        "sina": 0.5,
        "tencent": 0.2,
        "jqdata": 0.0,
        "tushare": 0.5,
        "baostock": 1.0,
    }
    with _instances_lock:
        if source_name not in _instances:
            interval = DEFAULT_INTERVALS.get(source_name, 1.0)
            _instances[source_name] = RateLimiter(min_interval=interval)
        return _instances[source_name]
