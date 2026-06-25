"""HTTP重试装饰器 + 人性化延迟"""

import time
import random
import functools
import threading
from collections import deque
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
    """数据源健康追踪器 — 自动检测接口可用性

    自适应退避：连续失败越多，恢复等待越长。
    - 3次失败 → 标记不可用（30min 恢复间隔）
    - 6次失败 → 60min
    - 10+次失败 → 120min
    """

    def __init__(self, fail_threshold: int = 3, recovery_threshold: int = 2,
                 recovery_interval: int = 1800):
        self.fail_threshold = fail_threshold
        self.recovery_threshold = recovery_threshold
        self.recovery_interval = recovery_interval
        self._fail_counts: dict[str, int] = {}
        self._total_fail_counts: dict[str, int] = {}
        self._success_after_fail: dict[str, int] = {}
        self._unavailable: set[str] = set()
        self._last_recovery_attempt: dict[str, float] = {}
        self._lock = threading.Lock()
        # 滚动 24h 历史（惰性淘汰，上限 10k 条）
        self._history: deque = deque(maxlen=10000)

    def record_success(self, endpoint: str):
        with self._lock:
            self._history.append((time.time(), endpoint, "success"))
            if endpoint in self._unavailable:
                self._success_after_fail[endpoint] = \
                    self._success_after_fail.get(endpoint, 0) + 1
                if self._success_after_fail[endpoint] >= self.recovery_threshold:
                    self._unavailable.discard(endpoint)
                    self._success_after_fail.pop(endpoint, None)
                    self._fail_counts.pop(endpoint, None)
                    self._total_fail_counts.pop(endpoint, None)
                    logger.info(f"接口恢复: {endpoint}")
            else:
                self._fail_counts.pop(endpoint, None)

    def record_failure(self, endpoint: str):
        with self._lock:
            self._history.append((time.time(), endpoint, "failure"))
            self._success_after_fail.pop(endpoint, None)
            total = self._total_fail_counts.get(endpoint, 0) + 1
            self._total_fail_counts[endpoint] = total
            self._fail_counts[endpoint] = self._fail_counts.get(endpoint, 0) + 1
            if self._fail_counts[endpoint] >= self.fail_threshold:
                self._unavailable.add(endpoint)
                logger.warning(f"接口标记不可用: {endpoint} (连续失败{self._fail_counts[endpoint]}次, 累计{total}次)")

    def is_available(self, endpoint: str) -> bool:
        return endpoint not in self._unavailable

    def should_attempt_recovery(self, endpoint: str) -> bool:
        """检查是否到了恢复时间。累计失败越多恢复间隔越长。"""
        now = time.time()
        last = self._last_recovery_attempt.get(endpoint, 0)

        # 自适应恢复间隔：根据累计失败次数指数增长
        total = self._total_fail_counts.get(endpoint, 0)
        if total >= 10:
            effective_interval = 7200  # 10+次失败 → 120min
        elif total >= 6:
            effective_interval = 3600  # 6~9次失败 → 60min
        else:
            effective_interval = self.recovery_interval  # <6次失败 → 30min
        return (now - last) >= effective_interval

    def mark_recovery_attempt(self, endpoint: str):
        with self._lock:
            self._last_recovery_attempt[endpoint] = time.time()

    def try_use(self, endpoint: str) -> bool:
        """原子检查-使用：检查可用性并标记正在使用。避免 TOCTOU。"""
        with self._lock:
            if endpoint in self._unavailable:
                if not self._should_attempt_recovery_unlocked(endpoint):
                    return False
                self._last_recovery_attempt[endpoint] = time.time()
            return True

    def _should_attempt_recovery_unlocked(self, endpoint: str) -> bool:
        """无锁版恢复检查（调用者须已持有 _lock）。"""
        now = time.time()
        last = self._last_recovery_attempt.get(endpoint, 0)
        total = self._total_fail_counts.get(endpoint, 0)
        if total >= 10:
            return (now - last) >= 7200
        elif total >= 6:
            return (now - last) >= 3600
        return (now - last) >= self.recovery_interval

    def success_rate(self, endpoint: str, window_hours: int = 24) -> float:
        """返回指定端点在滚动时间窗口内的成功率 [0, 1]。
        无记录时返回 1.0（默认可用）。
        """
        now = time.time()
        cutoff = now - window_hours * 3600
        total = 0
        good = 0
        # 惰性淘汰：从队头移除过期记录
        with self._lock:
            while self._history and self._history[0][0] < cutoff:
                self._history.popleft()
            for ts, ep, result in self._history:
                if ep == endpoint:
                    total += 1
                    if result == "success":
                        good += 1
        if total == 0:
            return 1.0
        return good / total

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


def human_delay(base_min: float = 0.8, base_max: float = 2.5,
                long_pause_every: int = 20, long_pause_max: float = 6.0):
    """人性化随机延迟，模拟真人操作间隔。

    Args:
        base_min: 常规最短延迟(秒)
        base_max: 常规最长延迟(秒)
        long_pause_every: 每N次请求后加一次长停顿
        long_pause_max: 长停顿最大秒数
    """
    # 基础随机延迟
    delay = random.uniform(base_min, base_max)

    # 偶尔加入长停顿（模拟思考/查阅）
    if random.randint(1, long_pause_every) == 1:
        delay += random.uniform(1.0, long_pause_max)

    time.sleep(delay)
