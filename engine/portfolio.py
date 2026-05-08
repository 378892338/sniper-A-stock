"""仓位管理 — 含停牌冻结仓计算"""

from dataclasses import dataclass, field

from core.logger import get_logger

logger = get_logger("engine.portfolio")


@dataclass
class Position:
    """单只股票持仓"""
    symbol: str
    name: str = ""
    shares: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    weight_pct: float = 0.0
    status: str = "NORMAL"  # NORMAL / FROZEN
    sector: str = ""  # 所属ETF分类指数


class Portfolio:
    """仓位管理器"""

    def __init__(self, total_capital: float):
        self.total_capital = total_capital  # 总资金（分母永远不变）
        self.positions: dict[str, Position] = {}  # {symbol: Position}

    def total_market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    def frozen_value(self) -> float:
        return sum(p.market_value for p in self.positions.values() if p.status == "FROZEN")

    def operable_value(self) -> float:
        """可操作资金"""
        return self.total_capital - self.frozen_value()

    def position_pct(self) -> float:
        """当前总仓位（含冻结）"""
        return self.total_market_value() / self.total_capital

    def frozen_pct(self) -> float:
        """冻结仓占比"""
        tv = self.total_market_value()
        if tv == 0:
            return 0.0
        return self.frozen_value() / tv

    def operable_position_pct(self) -> float:
        """可操作仓位"""
        return self.operable_value() / self.total_capital

    _SINGLE_CAP_MAP = {"牛市": 0.15, "震荡": 0.10, "偏弱": 0.10, "熊市": 0.0}

    def can_add_position(self, symbol: str, target_pct: float,
                         single_max_pct: float | None = None,
                         sector_max_pct: float = 0.30,
                         market_state: str = "") -> bool:
        """
        检查是否能加仓。

        - 单只股票上限: 牛市15% / 震荡10% / 偏弱10% / 熊市0%
        - 单个ETF分类指数上限: 总资金 × sector_max_pct (30%)
        - 分母永远是总资金，不会因为停牌而放大
        """
        if single_max_pct is None:
            single_max_pct = self._SINGLE_CAP_MAP.get(market_state, 0.15)
        target_value = self.total_capital * target_pct
        current_value = self.positions[symbol].market_value if symbol in self.positions else 0
        new_value = current_value + target_value

        # 单只上限
        if new_value > self.total_capital * single_max_pct:
            return False

        # ETF分类指数上限
        if symbol in self.positions:
            sector = self.positions[symbol].sector
            sector_value = sum(p.market_value for p in self.positions.values()
                              if p.sector == sector)
            if sector_value + target_value > self.total_capital * sector_max_pct:
                return False

        # 冻结仓占比检查
        if self.frozen_pct() > 0.5:
            logger.warning("冻结仓占比 > 50%，暂停新买入")
            return False

        return True

    def calculate_target_position(self, market_state: str,
                                  min_confidence: float = 1.0) -> float:
        """
        计算目标仓位。

        根据文档:
        - 牛市(3强): 基础 100%
        - 震荡(2强1弱): 基础 50%
        - 偏弱(1强2弱): 基础 30%
        - 熊市(0强): 基础 0%

        实际仓位 = 基础 × min(三市场可信度系数)
        """
        base_map = {
            "牛市": 1.0,
            "震荡": 0.50,
            "偏弱": 0.30,
            "熊市": 0.0,
        }
        base = base_map.get(market_state, 0.0)
        return base * min_confidence

    def calculate_downgrade_actions(self, new_target_pct: float) -> list[dict]:
        """
        降级过渡：从当前仓位降到目标仓位。
        返回卖出操作列表 [{symbol, action, pct}]
        """
        current_pct = self.operable_position_pct()
        if current_pct <= new_target_pct:
            return []

        reduce_pct = current_pct - new_target_pct
        actions = []

        # 排序：先卖评分低的
        active_positions = [p for p in self.positions.values() if p.status == "NORMAL"]
        active_positions = sorted(active_positions, key=lambda p: p.weight_pct, reverse=True)

        remaining = reduce_pct * self.total_capital
        for pos in active_positions:
            if remaining <= 0:
                break
            sell_value = min(pos.market_value, remaining)
            actions.append({
                "symbol": pos.symbol,
                "action": "卖出",
                "value": sell_value,
                "pct": sell_value / self.total_capital,
            })
            remaining -= sell_value

        return actions

    def update_position(self, symbol: str, market_value: float,
                        current_price: float = None):
        """更新持仓市值"""
        if symbol in self.positions:
            old = self.positions[symbol]
            self.positions[symbol] = Position(
                symbol=old.symbol, name=old.name, shares=old.shares,
                avg_cost=old.avg_cost, current_price=current_price or old.current_price,
                market_value=market_value,
                weight_pct=market_value / self.total_capital,
                status=old.status, sector=old.sector,
            )

    def mark_frozen(self, symbol: str):
        """标记停牌"""
        if symbol in self.positions:
            old = self.positions[symbol]
            self.positions[symbol] = Position(
                symbol=old.symbol, name=old.name, shares=old.shares,
                avg_cost=old.avg_cost, current_price=old.current_price,
                market_value=old.market_value, weight_pct=old.weight_pct,
                status="FROZEN", sector=old.sector,
            )
            logger.warning(f"持仓冻结(停牌): {symbol}")

    def mark_resumed(self, symbol: str):
        """标记复牌"""
        if symbol in self.positions:
            old = self.positions[symbol]
            self.positions[symbol] = Position(
                symbol=old.symbol, name=old.name, shares=old.shares,
                avg_cost=old.avg_cost, current_price=old.current_price,
                market_value=old.market_value, weight_pct=old.weight_pct,
                status="NORMAL", sector=old.sector,
            )
            logger.info(f"持仓恢复(复牌): {symbol}")

    def get_sector_weights(self) -> dict[str, float]:
        """获取各ETF分类指数仓位权重"""
        weights = {}
        for pos in self.positions.values():
            sector = pos.sector or "未分类"
            weights[sector] = weights.get(sector, 0) + pos.market_value
        return {k: v / self.total_capital for k, v in weights.items()}
