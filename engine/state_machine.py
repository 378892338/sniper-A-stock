"""
系统状态机 — IDLE/COOLING/SCANNING/HUNTING/HOLDING

文档状态转换规则:
         ┌──────────────────────┐
         │     IDLE (空仓)       │
         │  每周检查第一层        │
         │  第一层不通过→继续IDLE  │
         └──────────┬───────────┘
                    │ 第一层通过 → 检查冷却期
                    ▼
         ┌──────────────────────┐
         │    COOLING (冷却)     │
         │  等待冷却期结束        │
         └──────────┬───────────┘
                    │
         ┌──────────────────────┐
         │   SCANNING (扫描中)    │
         │  每周检查第二层         │
         └──────────┬───────────┘
                    │ 第二层通过
                    ▼
         ┌──────────────────────┐
         │   HUNTING (寻股)      │
         │  每日检查第三层         │
         └──────────┬───────────┘
                    │ 第三层有强势股
                    ▼
         ┌──────────────────────┐
         │   HOLDING (持仓)      │
         │  每日追踪已推股票      │
         └──────────────────────┘
"""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.logger import get_logger

logger = get_logger("engine.state_machine")


class SystemState(Enum):
    IDLE = "IDLE"           # 空仓
    COOLING = "COOLING"     # 冷却期
    SCANNING = "SCANNING"   # 扫描ETF分类指数
    HUNTING = "HUNTING"     # 寻找个股
    HOLDING = "HOLDING"     # 持仓中


@dataclass
class StateContext:
    """状态机上下文"""
    state: SystemState = SystemState.IDLE
    position_pct: float = 0.0
    target_position_pct: float = 0.0
    strong_sectors: list[str] = field(default_factory=list)
    weak_sectors: list[str] = field(default_factory=list)
    holding_stocks: list[str] = field(default_factory=list)
    frozen_stocks: list[str] = field(default_factory=list)  # 停牌冻结

    # 冷却期
    cooling_until: datetime | None = None
    recent_liquidations: int = 0  # 最近8周的清仓次数
    last_liquidation_date: datetime | None = None

    # 预警
    daily_alert_active: bool = False
    daily_alert_level: str = ""  # 黄色/橙色/红色
    buy_frozen: bool = False  # 买入冻结

    # 历史
    state_history: list[dict] = field(default_factory=list)
    transition_count: int = 0

    # 降仓/清仓追踪
    _prev_target: float | None = None
    _liquidation_times: list = field(default_factory=list)


class StateMachine:
    """系统状态机"""

    def __init__(self):
        self.ctx = StateContext()
        self._last_weekly_check: datetime | None = None
        logger.info("状态机初始化: IDLE")

    def current_state(self) -> SystemState:
        return self.ctx.state

    def transition(self, new_state: SystemState, reason: str = ""):
        old = self.ctx.state
        if old == new_state:
            return
        self.ctx.state = new_state
        self.ctx.transition_count += 1
        self.ctx.state_history.append({
            "from": old.value,
            "to": new_state.value,
            "reason": reason,
            "time": datetime.now().isoformat(),
        })
        logger.info(f"状态转换: {old.value} → {new_state.value} ({reason})")

    def weekly_check(self, layer1_passed: bool, layer2_passed: bool = False,
                     cross_level_jump: bool = False):
        """
        每周检查，推进状态。

        layer1_passed: 第一层是否通过（至少2强）
        layer2_passed: 第二层是否通过（有强势指数）
        cross_level_jump: 是否跨级跳（跨两级以上 + 三市技术面全过）→ 跳过冷却
        """
        self._last_weekly_check = datetime.now()

        if self.ctx.state == SystemState.IDLE:
            if layer1_passed:
                # 检查冷却期
                if self._in_cooling():
                    if cross_level_jump:
                        self.transition(SystemState.SCANNING, "跨级跳覆盖冷却")
                    else:
                        self.transition(SystemState.COOLING, "第一层通过，进入冷却期")
                else:
                    self.transition(SystemState.SCANNING, "第一层通过")

        elif self.ctx.state == SystemState.COOLING:
            if not layer1_passed:
                self.transition(SystemState.IDLE, "冷却期中第一层不通过")
            elif not self._in_cooling():
                self.transition(SystemState.SCANNING, "冷却期结束")
            elif cross_level_jump:
                self.transition(SystemState.SCANNING, "跨级跳覆盖冷却")
            # else: 继续等待冷却期

        elif self.ctx.state == SystemState.SCANNING:
            if not layer1_passed:
                self.transition(SystemState.IDLE, "第一层失效")
            elif layer2_passed and not self.ctx.buy_frozen:
                self.transition(SystemState.HUNTING, "第二层通过")
            # else: 继续扫描

        elif self.ctx.state == SystemState.HUNTING:
            if not layer1_passed:
                self._record_liquidation("第一层失效")
                self.transition(SystemState.IDLE, "第一层失效")
            elif not layer2_passed:
                self.transition(SystemState.SCANNING, "第二层失效")
            elif self.ctx.buy_frozen:
                pass  # 冻结买入，继续HUNTING但不出手
            # else: 继续寻股

        elif self.ctx.state == SystemState.HOLDING:
            if not layer1_passed:
                self._record_liquidation("第一层失效")
                self._start_cooling()
                self.transition(SystemState.COOLING if self._in_cooling() else SystemState.IDLE,
                                "第一层失效，清仓")
            elif not layer2_passed:
                # 部分清仓：记录弱指数供策略层减仓
                self.ctx.weak_sectors = self.ctx.strong_sectors.copy()
                self.ctx.strong_sectors = []
                logger.warning(f"L2失效，弱指数: {self.ctx.weak_sectors}，策略层将在下次调仓时减仓")
                self.transition(SystemState.HOLDING, "L2部分失效，持仓中等候调仓")
            # else: 继续持仓

    def on_position_opened(self, symbols: list[str]):
        """开仓时调用"""
        self.ctx.holding_stocks = self.ctx.holding_stocks + list(symbols)
        if self.ctx.state == SystemState.HUNTING:
            self.transition(SystemState.HOLDING, f"开仓 {len(symbols)} 只股票")

    def on_position_closed(self, symbols: list[str] = None, reason: str = ""):
        """平仓时调用"""
        if symbols:
            self.ctx.holding_stocks = [h for h in self.ctx.holding_stocks if h not in symbols]
            self.ctx.frozen_stocks = [f for f in self.ctx.frozen_stocks if f not in symbols]
        else:
            self.ctx.holding_stocks = []
            self.ctx.frozen_stocks = []

        if not self.ctx.holding_stocks:
            self._record_liquidation(reason)
            self._start_cooling()
            self.transition(
                SystemState.COOLING if self._in_cooling() else SystemState.IDLE,
                reason
            )

    def on_daily_alert(self, level: str):
        """日频预警触发"""
        self.ctx.daily_alert_active = True
        self.ctx.daily_alert_level = level
        self.ctx.buy_frozen = True
        logger.warning(f"日频预警: {level}")

    def on_daily_alert_cleared(self):
        """日频预警解除"""
        self.ctx.daily_alert_active = False
        self.ctx.daily_alert_level = ""
        self.ctx.buy_frozen = False
        logger.info("日频预警解除")

    def on_stock_frozen(self, symbol: str):
        """股票停牌冻结"""
        if symbol in self.ctx.holding_stocks:
            self.ctx.holding_stocks = [h for h in self.ctx.holding_stocks if h != symbol]
            self.ctx.frozen_stocks = self.ctx.frozen_stocks + [symbol]
            logger.warning(f"停牌冻结: {symbol}")
            if len(self.ctx.frozen_stocks) / max(len(self.ctx.holding_stocks) + len(self.ctx.frozen_stocks), 1) > 0.5:
                logger.error("冻结仓占比 > 50%，暂停新买入")

    def on_stock_resumed(self, symbol: str):
        """股票复牌"""
        if symbol in self.ctx.frozen_stocks:
            self.ctx.frozen_stocks = [f for f in self.ctx.frozen_stocks if f != symbol]
            self.ctx.holding_stocks = self.ctx.holding_stocks + [symbol]
            logger.info(f"复牌: {symbol}")

    def can_buy(self) -> bool:
        """是否可以买入"""
        if self.ctx.buy_frozen:
            return False
        if self.ctx.state in (SystemState.IDLE, SystemState.COOLING):
            return False
        frozen_ratio = len(self.ctx.frozen_stocks) / max(
            len(self.ctx.holding_stocks) + len(self.ctx.frozen_stocks), 1
        )
        if frozen_ratio > 0.5:
            return False
        return True

    def _in_cooling(self) -> bool:
        """是否在冷却期内"""
        if self.ctx.cooling_until is None:
            return False
        return datetime.now() < self.ctx.cooling_until

    def _start_cooling(self):
        """启动冷却期"""
        liquidations = self.ctx.recent_liquidations + 1

        if liquidations >= 3:
            # 中期冷却：4周
            self.ctx.cooling_until = datetime.now() + timedelta(weeks=4)
            logger.warning(f"中期冷却: 4周 (8周内{liquidations}次清仓)")
        else:
            # 短期冷却：1周
            self.ctx.cooling_until = datetime.now() + timedelta(weeks=1)
            logger.info(f"短期冷却: 1周")

    def _record_liquidation(self, reason: str = ""):
        """记录清仓事件（8周滑动窗口）"""
        now = datetime.now()
        cutoff = now - timedelta(weeks=8)
        self.ctx._liquidation_times = self.ctx._liquidation_times + [now]
        self.ctx._liquidation_times = [t for t in self.ctx._liquidation_times if t >= cutoff]
        self.ctx.recent_liquidations = len(self.ctx._liquidation_times)
        self.ctx.last_liquidation_date = now
        logger.info(f"记录清仓: {reason} (8周内累计{self.ctx.recent_liquidations}次)")

    def get_status_report(self) -> dict:
        """获取当前状态报告"""
        return {
            "state": self.ctx.state.value,
            "position_pct": self.ctx.position_pct,
            "target_position_pct": self.ctx.target_position_pct,
            "holding_stocks": self.ctx.holding_stocks,
            "frozen_stocks": self.ctx.frozen_stocks,
            "daily_alert": self.ctx.daily_alert_level if self.ctx.daily_alert_active else "无",
            "buy_frozen": self.ctx.buy_frozen,
            "cooling_until": self.ctx.cooling_until.isoformat() if self.ctx.cooling_until else None,
            "transitions": self.ctx.transition_count,
        }
