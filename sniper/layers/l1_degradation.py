"""L1 降级仲裁器 — 多故障优先级仲裁 + 状态机持久化

设计原则:
  1. resolve()在任何故障组合下输出唯一确定性状态
  2. escalation_only: 降级只能升不能降,恢复走独立_recovery_check
  3. 所有退出条件全量化(评审FAIL-15修复)
  4. 状态持久化到degradation_YYYYMMDD.jsonl
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
from pathlib import Path

from sniper.config import DEGRADATION as CFG
from core.logger import get_logger

logger = get_logger("sniper.layers.l1_degradation")


class DegradationLevel(str, Enum):
    GREEN = "GREEN"       # 正常运行
    YELLOW = "YELLOW"     # PARTIAL降级
    ORANGE = "ORANGE"     # FALLBACK降级
    RED = "RED"           # 熔断


class DegradationArbiter:
    """多故障优先级仲裁器。

    输入各故障源独立严重度,输出唯一确定性降级状态。

    用法:
        arbiter = DegradationArbiter()
        state = arbiter.resolve({"etf_api": "CRITICAL", "l0": "MAJOR"})
        # state = "ORANGE"
    """

    # 故障源 -> 预期降级级别 映射(不同CRITICAL源映射不同级别)
    SOURCE_LEVEL_MAP = {
        "etf_api": "YELLOW",      # CRITICAL: ETF API不可达
        "etf_all_missing": "ORANGE", # CRITICAL: 全部ETF缺失
        "db_disconnect": "RED",    # CRITICAL: DB断连
        "data_corrupt": "RED",     # CRITICAL: 数据损坏
        "disk_full": "RED",        # CRITICAL: 磁盘满
        "config_corrupt": "ORANGE", # CRITICAL: 配置损坏
        "clock_skew": "ORANGE",    # MAJOR: 时钟回拨
        "l0_anomaly": "YELLOW",    # MAJOR: L0异常
        "router_timeout": "YELLOW", # MAJOR: 路由超时
        "cache_corrupt": "YELLOW", # MAJOR: 缓存损坏
        "etf_partial": "GREEN",    # MINOR: 部分ETF缺失(不计入状态)
    }
    SEVERITY_PRIORITY = {"CRITICAL": 4, "MAJOR": 3, "MINOR": 2, "NONE": 1}
    LEVEL_ORDER = {"RED": 4, "ORANGE": 3, "YELLOW": 2, "GREEN": 1}

    def __init__(self, config: dataclass | None = None,
                 log_dir: str | None = None):
        self.cfg = config or CFG
        self._current_level: str = "GREEN"
        self._manual_reset: bool = False
        self._recovery_counters: dict[str, int] = {
            "green_ready": 0,
            "yellow_ready": 0,
            "orange_ready": 0,
        }
        self._log_dir = Path(log_dir or "outputs/degradation")
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def set_manual_reset(self, value: bool) -> None:
        """设置人工确认标记(仅RED->ORANGE时使用)"""
        self._manual_reset = value

    def ingest_signal(self, source: str, severity: str,
                      level_override: str | None = None) -> None:
        """注入单个故障信号(外部模块调用)

        Args:
            source: 故障源标识(如"etf_api", "db_disconnect")
            severity: 严重度("CRITICAL"/"MAJOR"/"MINOR"/"NONE")
            level_override: 可选,指定直接降级目标级别,不传则使用SOURCE_LEVEL_MAP
        """
        new_level = level_override or self.SOURCE_LEVEL_MAP.get(source, "YELLOW")

        self._log_event({
            "event": "FAULT_SIGNAL",
            "source": source,
            "severity": severity,
            "new_level": new_level,
        })

    def resolve(self, fault_signals: dict[str, str]) -> str:
        """输入各故障源的状态级别,输出唯一确定性状态。

        Args:
          fault_signals: {"etf": "ORANGE", "l0": "YELLOW", ...}
            值是期望的降级级别("RED"/"ORANGE"/"YELLOW"/"GREEN")

        Returns:
          "RED" / "ORANGE" / "YELLOW" / "GREEN"
        """
        # 取最高严重度的级别
        levels = list(fault_signals.values())
        requested = max(levels, key=lambda x: self.LEVEL_ORDER.get(x, 1)) if levels else "GREEN"

        # RED状态保护: 必须人工确认,不自动恢复
        if self._current_level == "RED" and requested != "RED":
            if not self._manual_reset:
                requested = "RED"

        # escalation_only: 只升不降,恢复走_recovery_check
        new_level = self._escalate_only(self._current_level, requested)

        # 检查是否可以从降级状态恢复
        if new_level != requested or new_level == self._current_level:
            recovery = self._check_recovery(fault_signals)
            if recovery is not None:
                new_level = recovery

        self._current_level = new_level
        self._log_transition(self._current_level, fault_signals)
        return self._current_level

    def _escalate_only(self, current: str, requested: str) -> str:
        """降级只升不降。"""
        if self.LEVEL_ORDER.get(requested, 1) > self.LEVEL_ORDER.get(current, 1):
            return requested
        return current

    def _check_recovery(self, fault_signals: dict[str, str]) -> str | None:
        """恢复条件检查。

        各level退出条件:
          YELLOW->GREEN: 连续N个交易日全正常+数据质量校验通过
          ORANGE->YELLOW: 连续N个交易日全正常
          RED->ORANGE: 人工确认+连续N个交易日全正常
        """
        all_normal = all(s == "NONE" for s in fault_signals.values())

        if self._current_level == "YELLOW" and all_normal:
            self._recovery_counters["green_ready"] += 1
            if self._recovery_counters["green_ready"] >= self.cfg.yellow_to_green_days:
                self._reset_counters()
                return "GREEN"
        elif self._current_level == "ORANGE" and all_normal:
            self._recovery_counters["yellow_ready"] += 1
            if self._recovery_counters["yellow_ready"] >= self.cfg.orange_to_yellow_days:
                self._recovery_counters["green_ready"] = 0
                return "YELLOW"
        elif self._current_level == "RED":
            self._recovery_counters["orange_ready"] += 1
            if self._manual_reset and self._recovery_counters["orange_ready"] >= self.cfg.red_to_orange_days:
                self._reset_counters()
                self._manual_reset = False
                return "ORANGE"
        else:
            # 不在降级状态,重置计数器
            self._reset_counters()

        # 未满足恢复条件
        if not all_normal:
            self._reset_counters()
        return None

    def _reset_counters(self) -> None:
        for k in self._recovery_counters:
            self._recovery_counters[k] = 0

    def get_current_level(self) -> str:
        return self._current_level

    def get_smooth_w_etf_ratio(self, requested_ratio: float = 1.0) -> float:
        """恢复过渡期的w_etf缩放。

        在ORANGE->YELLOW恢复过渡期,返回缩放后权重(评审FAIL-15修复)
        """
        if self._current_level == "GREEN":
            return 1.0
        if self._current_level == "YELLOW":
            return self.cfg.orange_to_yellow_w_etf_ratio
        if self._current_level == "ORANGE":
            return 0.0  # 纯SW1,不贡献
        return 0.0  # RED跳过融合

    # ── 日志与持久化 ──

    def _log_transition(self, new_level: str,
                        fault_signals: dict[str, str]) -> None:
        if new_level != self._current_level:
            logger.info(
                f"降级状态转换: {self._current_level} -> {new_level} | "
                f"信号={fault_signals}"
            )

    def _log_event(self, event: dict) -> None:
        event["timestamp"] = datetime.now().isoformat()
        # 按日期分文件
        date_str = datetime.now().strftime("%Y%m%d")
        log_file = self._log_dir / f"degradation_{date_str}.jsonl"

        line = json.dumps(event, ensure_ascii=False)
        # 约束单行长度(评审FAIL-14修复)
        if len(line) > self.cfg.max_line_bytes:
            line = json.dumps({k: str(v)[:200] for k, v in event.items()},
                              ensure_ascii=False)

        # 原子写入: 先写.tmp再rename
        tmp_file = log_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            tmp_file.replace(log_file)  # 原子rename
        except OSError as e:
            logger.error(f"降级日志写入失败(可能磁盘满): {e}")
