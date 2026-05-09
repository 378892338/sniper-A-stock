"""HTTP重试装饰器"""

import time
import random
import functools
import threading
from core.logger import get_logger

logger = get_logger("shared.retry")


def retry(max_retries: int = 3, base_delay: float = 1.0, backoff: float = 2.0):
    """指数退避重试装饰器"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (backoff ** attempt) + random.uniform(0, 1.0)
                        logger.warning(
                            f"{func.__name__} 第{attempt+1}次失败: {e}, "
                            f"{delay:.1f}s后重试"
                        )
                        time.sleep(delay)
            logger.error(f"{func.__name__} 重试{max_retries}次后仍失败: {last_exc}")
            raise last_exc
        return wrapper
    return decorator


class HealthTracker:
    """数据源健康追踪器 — 自动检测接口可用性"""

    def __init__(self, fail_threshold: int = 3, recovery_threshold: int = 2,
                 recovery_interval: int = 1800):
        self.fail_threshold = fail_threshold
        self.recovery_threshold = recovery_threshold
        self.recovery_interval = recovery_interval  # 30分钟
        self._fail_counts: dict[str, int] = {}
        self._success_after_fail: dict[str, int] = {}
        self._unavailable: set[str] = set()
        self._last_recovery_attempt: dict[str, float] = {}
        self._lock = threading.Lock()

    def record_success(self, endpoint: str):
        with self._lock:
            if endpoint in self._unavailable:
                self._success_after_fail[endpoint] = \
                    self._success_after_fail.get(endpoint, 0) + 1
                if self._success_after_fail[endpoint] >= self.recovery_threshold:
                    self._unavailable.discard(endpoint)
                    self._success_after_fail.pop(endpoint, None)
                    self._fail_counts.pop(endpoint, None)
                    logger.info(f"接口恢复: {endpoint}")
            else:
                self._fail_counts.pop(endpoint, None)

    def record_failure(self, endpoint: str):
        with self._lock:
            self._success_after_fail.pop(endpoint, None)
            self._fail_counts[endpoint] = self._fail_counts.get(endpoint, 0) + 1
            if self._fail_counts[endpoint] >= self.fail_threshold:
                self._unavailable.add(endpoint)
                logger.warning(f"接口标记不可用: {endpoint} (连续失败{self._fail_counts[endpoint]}次)")

    def is_available(self, endpoint: str) -> bool:
        return endpoint not in self._unavailable

    def should_attempt_recovery(self, endpoint: str) -> bool:
        now = time.time()
        last = self._last_recovery_attempt.get(endpoint, 0)
        return (now - last) >= self.recovery_interval

    def mark_recovery_attempt(self, endpoint: str):
        with self._lock:
            self._last_recovery_attempt[endpoint] = time.time()

    @property
    def unavailable_endpoints(self) -> set[str]:
        return self._unavailable.copy()


# 全局健康追踪器实例
health_tracker = HealthTracker()


def check_and_attempt_recovery(endpoint: str) -> bool:
    """数据源调用前检查：健康则放行，到期则尝试恢复。返回 True 表示可继续。"""
    if health_tracker.is_available(endpoint):
        return True
    if health_tracker.should_attempt_recovery(endpoint):
        logger.info(f"尝试恢复接口: {endpoint}")
        health_tracker.mark_recovery_attempt(endpoint)
        return True
    return False
