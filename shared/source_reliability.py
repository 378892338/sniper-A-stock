"""数据源可靠性引擎 — 四层防御的后三层

Layer 2: StockSkipTracker       — 个股级智能跳过
Layer 3: SourceReliabilityTracker — 源可靠性贝叶斯评分 + 自适应排序
Layer 4: CoverageState          — 管道级覆盖率状态机（含 HALF_OPEN 恢复路径）

用法:
  from shared.source_reliability import StockSkipTracker, CoverageState, SourceReliabilityTracker

  skip = StockSkipTracker()
  coverage = CoverageState()
  tracker = SourceReliabilityTracker()
"""

import json
import math
import threading
import time
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════
# Layer 2: StockSkipTracker  — 个股级智能跳过
# ═══════════════════════════════════════════════════════════

class StockSkipTracker:
    """个股级智能跳过 — 不无限重试确定不可获取的股票

    设计要点:
      1) 线程安全（threading.Lock），兼容 ThreadPoolExecutor 并发
      2) 持久化（先写 .tmp 后 rename 到目标），进程重启不丢失
      3) TTL（默认 24h）— 跳过记录过期后自动复活，不永久排除
      4) 源恢复时 reset_all() 通知跳过列表清空

    用法:
      skip = StockSkipTracker()
      skip.record_batch({"000001": True, "000002": False, ...})
      if skip.should_skip("000003"):
          pass  # 跳过
      skip.reset_all()  # 数据源恢复时调用
    """

    def __init__(
        self,
        max_attempts: int = 3,
        skip_ttl: float = 86400.0,  # 24h
        persist_path: str = "outputs/reports/skip_list.json",
    ):
        self.max_attempts = max_attempts
        self.skip_ttl = skip_ttl
        self._persist_path = Path(persist_path)

        self._lock = threading.Lock()
        self._fail_counts: dict[str, int] = {}        # symbol → consecutive_fails
        self._skip_set: dict[str, float] = {}          # symbol → skipped_at_timestamp

        self._load()

    # ── 公开 API ──

    def record_batch(self, results: dict[str, bool]) -> None:
        """线程安全地记录一批股票的成败。

        Args:
            results: {symbol: success} — True=成功，False=失败
        """
        with self._lock:
            now = time.time()
            for symbol, success in results.items():
                if success:
                    self._fail_counts.pop(symbol, None)
                    self._skip_set.pop(symbol, None)
                else:
                    count = self._fail_counts.get(symbol, 0) + 1
                    self._fail_counts[symbol] = count
                    if count >= self.max_attempts:
                        self._skip_set[symbol] = now
            self._save()

    def should_skip(self, symbol: str) -> bool:
        """检查个股是否应跳过。

        TTL 过期自动复活：超过 skip_ttl 未再次失败则移出跳过列表。
        """
        with self._lock:
            ts = self._skip_set.get(symbol)
            if ts is None:
                return False
            if time.time() - ts > self.skip_ttl:
                del self._skip_set[symbol]
                self._fail_counts.pop(symbol, None)
                self._save()
                return False
            return True

    def get_skip_list(self) -> set[str]:
        """返回当前跳过的个股集合（快照，不含过期检查）。"""
        with self._lock:
            return set(self._skip_set.keys())

    def get_skip_count(self) -> int:
        """当前跳过的个股数量。"""
        with self._lock:
            return len(self._skip_set)

    def reset_all(self) -> None:
        """数据源恢复时调用 — 清空跳过列表并持久化。"""
        with self._lock:
            self._fail_counts.clear()
            self._skip_set.clear()
            self._save()

    def get_fail_count(self, symbol: str) -> int:
        """查询个股当前连续失败次数。"""
        with self._lock:
            return self._fail_counts.get(symbol, 0)

    # ── 持久化 ──

    def _load(self) -> None:
        if not self._persist_path.exists():
            return
        try:
            raw = self._persist_path.read_text(encoding="utf-8").strip()
            if not raw:
                return
            data = json.loads(raw)
            self._fail_counts = data.get("fail_counts", {}) or {}
            self._skip_set = data.get("skip_set", {}) or {}
            # 转换 skip_set 的 key 为 str，value 为 float
            self._skip_set = {str(k): float(v) for k, v in self._skip_set.items()}
        except (json.JSONDecodeError, OSError, ValueError):
            # 损坏文件静默重置
            self._fail_counts = {}
            self._skip_set = {}

    def _save(self) -> None:
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persist_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps({
                    "fail_counts": self._fail_counts,
                    "skip_set": self._skip_set,
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            # 原子替换
            tmp_path.replace(self._persist_path)
        except OSError:
            pass  # 写失败不阻塞管道


# ═══════════════════════════════════════════════════════════
# Layer 4: CoverageState  — 覆盖率监控状态机
# ═══════════════════════════════════════════════════════════

class CoverageState:
    """覆盖率监控状态机 — 替代固定重试死循环

    状态转移:

        ┌─────────────────────────────────────────┐
        │                                         │
        ▼                                         │
     SYNCING ──(3轮无增长)──→ DIMINISHING ──→ DEGRADED
        │                         │                │
        │  (覆盖≥95%)              │  (跳升>15%)    │  (每 K 轮探活)
        └─────────────────────────┴────────────────┘
                                                   │
                                                   ▼
                                              HALF_OPEN
                                                   │
                                              ┌────┴────┐
                                              │         │
                                          (成功)    (失败)
                                              │         │
                                              ▼         │
                                           SYNCING     │
                                                       │
                                               回 DEGRADED

    用法:
      cs = CoverageState()
      while True:
          cov = get_coverage()
          status = cs.update(cov)
          if status == cs.OK: break
          if status == cs.DEGRADED: break
          if status == cs.FAILED: break
          # 继续同步
    """

    # 状态常量
    OK = "ok"                 # 覆盖率达标，结束
    SYNCING = "syncing"       # 正常同步中
    DIMINISHING = "diminishing"  # 无增长中
    DEGRADED = "degraded"     # 接受最佳可用覆盖
    FAILED = "failed"         # 所有源不可用
    HALF_OPEN = "half_open"   # 探活中

    def __init__(
        self,
        acceptable_threshold: float = 0.95,
        max_flat_rounds: int = 3,
        max_total_rounds: int = 5,
        degraded_threshold: float = 0.30,
        jump_threshold: float = 0.15,
        drift_threshold: float = 0.05,
        probe_interval: int = 5,  # HALF_OPEN 每 N 轮探活一次
    ):
        self.acceptable_threshold = acceptable_threshold
        self.max_flat_rounds = max_flat_rounds
        self.max_total_rounds = max_total_rounds
        self.degraded_threshold = degraded_threshold
        self.jump_threshold = jump_threshold
        self.drift_threshold = drift_threshold
        self.probe_interval = probe_interval

        self.state = self.SYNCING
        self.rounds = 0
        self.rounds_without_growth = 0
        self.rounds_in_degraded = 0
        self.prev_state: str | None = None
        self.coverage_history: list[float] = []
        self.best_coverage = 0.0
        self.prev_coverage: float | None = None

    def update(self, coverage: float) -> str:
        """输入当前覆盖率，更新状态机。返回当前状态。

        Returns:
            "ok" — 覆盖率达标，管道已完成
            "syncing" — 继续同步（覆盖率在增长）
            "diminishing" — 连续多轮无增长，准备降级
            "degraded" — 已接受最佳覆盖，管道可继续（标注降级）
            "failed" — 所有源不可用，管道可继续（标注失败）
            "half_open" — 探活中（来自 degraded/failed 的恢复试探）
        """
        self.rounds += 1
        self.coverage_history.append(coverage)

        # ── 前进状态：先记录前一次状态 ──
        self.prev_state = self.state

        # ── 覆盖率跳升检测（从 degraded 恢复） ──
        if self.state in (self.DEGRADED, self.FAILED, self.HALF_OPEN):
            if coverage - self.best_coverage > self.jump_threshold:
                self.state = self.SYNCING
                self.best_coverage = coverage
                self.rounds_without_growth = 0
                self.rounds_in_degraded = 0
                return self.state

        # ── 覆盖率回退检测（覆盖率先高后低） ──
        if self.prev_coverage is not None:
            drift = self.prev_coverage - coverage
            if drift > self.drift_threshold and self.state == self.SYNCING:
                self.state = self.SYNCING  # 保持但记录
                self.best_coverage = coverage
                self.rounds_without_growth = 0
                self.prev_coverage = coverage
                return self.state

        self.prev_coverage = coverage

        # ── 更新最佳覆盖率和无增长计数 ──
        if coverage > self.best_coverage:
            self.best_coverage = coverage
            self.rounds_without_growth = 0
        else:
            self.rounds_without_growth += 1

        # ── HALF_OPEN 状态转移 ──
        if (self.state in (self.DEGRADED, self.FAILED)
                and self.rounds_in_degraded >= self.probe_interval):
            self.rounds_in_degraded = 0
            self.state = self.HALF_OPEN
            return self.state

        # ── 状态转移链 ──

        # OK：覆盖率达标
        if coverage >= self.acceptable_threshold:
            self.state = self.OK
            return self.state

        # 从 SYNCING 进入 DIMINISHING
        if self.state == self.SYNCING and self.rounds_without_growth >= self.max_flat_rounds:
            self.state = self.DIMINISHING
            return self.state

        # 从 DIMINISHING 进入 DEGRADED/FAILED
        if self.state == self.DIMINISHING and self.rounds_without_growth >= self.max_flat_rounds:
            self.state = self.DEGRADED if coverage > self.degraded_threshold else self.FAILED
            return self.state

        # 最大轮次熔断
        if self.rounds >= self.max_total_rounds and self.state == self.SYNCING:
            self.state = self.DEGRADED if coverage > self.degraded_threshold else self.FAILED
            return self.state

        # 在 DEGRADED 中计数
        if self.state in (self.DEGRADED, self.FAILED):
            self.rounds_in_degraded += 1

        return self.state

    def is_terminal(self) -> bool:
        """状态机是否已达到终态（OK / DEGRADED / FAILED / HALF_OPEN）"""
        return self.state in (self.OK, self.DEGRADED, self.FAILED, self.HALF_OPEN)

    def summary(self) -> dict[str, Any]:
        """返回状态摘要用于日志和监控。"""
        return {
            "state": self.state,
            "rounds": self.rounds,
            "best_coverage": round(self.best_coverage, 4),
            "current_coverage": round(self.coverage_history[-1], 4) if self.coverage_history else 0,
            "flat_rounds": self.rounds_without_growth,
        }


# ═══════════════════════════════════════════════════════════
# Layer 3: SourceReliabilityTracker  — 源可靠性评分
# ═══════════════════════════════════════════════════════════

class SourceReliabilityTracker:
    """数据源可靠性贝叶斯评分 + 自适应排序

    评分公式（贝叶斯平均 + 指数衰减窗口）:

        score = (weighted_successes + C * prior) / (weighted_total + C)

        其中:
          weighted_total = Σ(decay^(days_ago) × 1)
          weighted_successes = Σ(decay^(days_ago) × success)
          C = 5 (贝叶斯先验权重)
          prior = 0.7 (先验概率)
          decay = 0.95 (指数衰减，~14天半衰期)

    排序逻辑:
      高评分源(≥0.8) → 按分降序，优先使用
      中评分源(0.4-0.8) → 按分降序
      低评分源(<0.4) → 排到最后，每 N 轮探活一次
    """

    def __init__(
        self,
        prior: float = 0.7,
        bayesian_C: float = 5.0,
        decay: float = 0.95,
        probe_interval: int = 5,
        persist_path: str = "outputs/reports/source_reliability.json",
    ):
        self.prior = prior
        self.bayesian_C = bayesian_C
        self.decay = decay
        self.probe_interval = probe_interval
        self._persist_path = Path(persist_path)

        self._lock = threading.Lock()
        # endpoint → {success_weighted, total_weighted, last_run, consecutive_low}
        self._records: dict[str, dict[str, Any]] = {}
        self._round_since_last_probe: dict[str, int] = {}

        self._load()

    # ── 公开 API ──

    def record_result(self, endpoint: str, success: bool) -> None:
        """记录一次调用结果。

        Args:
            endpoint: 数据源端点名（如 "eastmoney_price"）
            success: True=成功, False=失败
        """
        with self._lock:
            rec = self._records.setdefault(endpoint, {
                "success_weighted": 0.0,
                "total_weighted": 0.0,
                "last_run": "",
                "consecutive_low": 0,
            })

            rec["total_weighted"] += 1.0
            if success:
                rec["success_weighted"] += 1.0
            rec["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")

            # 每日衰减
            self._apply_decay(endpoint)

            self._save()

    def get_score(self, endpoint: str) -> float:
        """获取指定端点的贝叶斯评分 [0.0, 1.0]"""
        with self._lock:
            rec = self._records.get(endpoint)
            if rec is None:
                return self.prior
            self._apply_decay(endpoint)
            total = rec["total_weighted"]
            if total <= 0:
                return self.prior
            success = rec["success_weighted"]
            return (success + self.bayesian_C * self.prior) / (total + self.bayesian_C)

    def get_all_scores(self) -> dict[str, float]:
        """获取所有端点的评分快照"""
        with self._lock:
            result = {}
            for ep in list(self._records.keys()):
                result[ep] = self.get_score(ep)
            return result

    def reorder_sources(self, current_order: list[str]) -> list[str]:
        """按可靠性评分自适应重排数据源顺序。

        Args:
            current_order: 原始的 DATA_SOURCE_PREFERENCE 顺序

        Returns:
            重排后的顺序（高→中→低三档，档内降序）
        """
        scores = self.get_all_scores()

        high = [(s, scores.get(s, self.prior)) for s in current_order]
        mid = []
        low = []

        for s, sc in high:
            if sc >= 0.8:
                high.append((s, sc))
            elif sc >= 0.4:
                mid.append((s, sc))
            else:
                low.append(s)

        high.sort(key=lambda x: -x[1])
        mid.sort(key=lambda x: -x[1])

        return [s for s, _ in high] + [s for s, _ in mid] + low

    def should_probe(self, endpoint: str) -> bool:
        """低分源是否到了探活轮次。

        防止永不知情（probe_interval=0 时永不探活）。
        """
        if self.probe_interval <= 0:
            return False
        score = self.get_score(endpoint)
        if score >= 0.4:
            return True  # 不是低分源，正常使用
        count = self._round_since_last_probe.get(endpoint, 0) + 1
        self._round_since_last_probe[endpoint] = count
        return count >= self.probe_interval

    def mark_probed(self, endpoint: str) -> None:
        """标记已探活，重置探活计数器。"""
        self._round_since_last_probe[endpoint] = 0

    def reset(self) -> None:
        """完全重置所有评分。"""
        with self._lock:
            self._records.clear()
            self._round_since_last_probe.clear()
            self._save()

    # ── 内部 ──

    def _apply_decay(self, endpoint: str) -> None:
        """对指定端点的权重执行每日衰减。

        每调用一次，历史权重乘以 decay^(days_since_last_update)。
        近似为：每次 record_result 时做一次微衰减。
        """
        rec = self._records.get(endpoint)
        if rec is None:
            return
        # 使用当前 total_weighted 作为"时间"代理：每 100 次调用衰减一次
        factor = max(self.decay, 0.5)
        if rec["total_weighted"] > 100:
            rec["total_weighted"] *= factor
            rec["success_weighted"] *= factor

    def _load(self) -> None:
        if not self._persist_path.exists():
            return
        try:
            raw = self._persist_path.read_text(encoding="utf-8").strip()
            if not raw:
                return
            self._records = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            self._records = {}

    def _save(self) -> None:
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persist_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(self._records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._persist_path)
        except OSError:
            pass
