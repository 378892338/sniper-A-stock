"""仓位管理与组合风控"""

import sniper.config as CFG
from core.logger import get_logger

logger = get_logger("sniper.engine.risk")


class RiskManager:
    """组合风控管理器。跟踪持仓、计算仓位、执行风控规则。"""

    def __init__(self, initial_capital: float = 1_000_000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.peak_capital = initial_capital
        self.positions: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.daily_values: list[dict] = []
        self._max_positions = CFG.RISK.max_positions

    def set_max_positions(self, n: int):
        """动态调整最大持仓数。"""
        self._max_positions = max(1, min(CFG.RISK.max_positions, n))

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def total_exposure(self) -> float:
        return sum(p["market_value"] for p in self.positions.values())

    @property
    def total_capital(self) -> float:
        return self.cash + self.total_exposure

    def can_open_new(self) -> bool:
        if self.position_count >= self._max_positions:
            return False
        if self.cash <= 0:
            return False
        return True

    def get_remaining_budget(self) -> float:
        """计算还可动用多少资金以达到目标总暴露。

        目标暴露 = total_capital × target_exposure_ratio
        剩余预算 = max(0, 目标暴露 - 当前总暴露)
        """
        target = self.total_capital * CFG.RISK.target_exposure_ratio
        return max(0.0, target - self.total_exposure)

    def get_position_size(self, scalar: float = 1.0) -> float:
        """兼容旧接口，返回按目标暴露均摊的单笔预算。

        在新的仓位管理模式下，外部应直接使用 get_remaining_budget() +
        per-stock 均摊方式。此方法保留供旧调用方使用。
        """
        remaining = self.get_remaining_budget()
        slots = max(1, self._max_positions - self.position_count)
        return remaining / slots

    def open_position(self, symbol: str, price: float, date: str,
                      sector: str = "", score: float = 0.0,
                      size_scalar: float = 1.0,
                      allocated_budget: float | None = None) -> dict | None:
        """开仓。

        Args:
            allocated_budget: 该笔仓位的预算金额。
              传入后忽略 size_scalar。为 None 时用 get_position_size() 计算。
        """
        if not self.can_open_new():
            return None

        if allocated_budget is not None:
            size = allocated_budget
        else:
            size = self.get_position_size(size_scalar)

        shares = int(size / price / 100) * 100
        if shares < 100:
            return None
        trade_value = shares * price
        cost = trade_value * (1 + CFG.BACKTEST.commission_buy + CFG.BACKTEST.slippage)
        if cost > self.cash:
            return None

        self.cash -= cost
        pos = {
            "symbol": symbol, "shares": shares, "entry_price": price,
            "entry_date": date, "cost": cost, "market_value": trade_value,
            "highest_price": price, "sector": sector, "score": score,
            "pnl": 0.0, "pnl_pct": 0.0,
        }
        self.positions[symbol] = pos
        self.trades.append({
            "date": date, "symbol": symbol, "action": "BUY",
            "price": price, "shares": shares, "cost": cost,
            "commission": trade_value * CFG.BACKTEST.commission_buy,
            "slippage": trade_value * CFG.BACKTEST.slippage,
        })
        logger.info(f"[风控] 开仓 {symbol} {shares}股 @ {price:.2f}, 成本 {cost:.0f}")
        return pos

    def close_position(self, symbol: str, price: float, date: str,
                       reason: str = "") -> dict | None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        trade_value = pos["shares"] * price
        sell_cost_rate = CFG.BACKTEST.commission_sell + CFG.BACKTEST.stamp_duty + CFG.BACKTEST.slippage
        proceeds = trade_value * (1 - sell_cost_rate)
        pnl = proceeds - pos["cost"]
        self.cash += proceeds
        self.trades.append({
            "date": date, "symbol": symbol, "action": "SELL",
            "price": price, "shares": pos["shares"],
            "cost": pos["cost"], "proceeds": proceeds,
            "commission": trade_value * CFG.BACKTEST.commission_sell,
            "stamp_duty": trade_value * CFG.BACKTEST.stamp_duty,
            "slippage": trade_value * CFG.BACKTEST.slippage,
            "pnl": pnl, "reason": reason,
            "sector": pos.get("sector", ""),
            "entry_date": pos.get("entry_date", ""),
            "entry_price": pos.get("entry_price", 0),
        })
        logger.info(f"[风控] 平仓 {symbol} {reason}: PnL {pnl:.0f} ({pnl/pos['cost']:.2%})")
        return {"pnl": pnl, "pnl_pct": pnl / pos["cost"]}

    def reduce_position(self, symbol: str, price: float, date: str,
                        ratio: float = 0.5, reason: str = "") -> dict | None:
        """减仓指定比例，保留剩余仓位。牛市环境下替代全仓止损/止盈。"""
        pos = self.positions.get(symbol)
        if pos is None:
            return None

        sell_shares = int(pos["shares"] * ratio / 100) * 100
        if sell_shares < 100:
            return None

        cost_per_share = pos["cost"] / pos["shares"]
        sell_cost = cost_per_share * sell_shares

        trade_value = sell_shares * price
        sell_cost_rate = CFG.BACKTEST.commission_sell + CFG.BACKTEST.stamp_duty + CFG.BACKTEST.slippage
        proceeds = trade_value * (1 - sell_cost_rate)
        pnl = proceeds - sell_cost
        self.cash += proceeds

        self.trades.append({
            "date": date, "symbol": symbol, "action": "SELL",
            "price": price, "shares": sell_shares,
            "cost": sell_cost, "proceeds": proceeds,
            "commission": trade_value * CFG.BACKTEST.commission_sell,
            "stamp_duty": trade_value * CFG.BACKTEST.stamp_duty,
            "slippage": trade_value * CFG.BACKTEST.slippage,
            "pnl": pnl, "reason": f"{reason}(减半仓)",
            "sector": pos.get("sector", ""),
            "entry_date": pos.get("entry_date", ""),
            "entry_price": pos.get("entry_price", 0),
        })

        # 更新剩余仓位
        pos["shares"] -= sell_shares
        pos["cost"] = cost_per_share * pos["shares"]
        pos["market_value"] = pos["shares"] * price

        logger.info(
            f"[风控] 减仓 {symbol} {sell_shares}股 @ {price:.2f}, "
            f"PnL {pnl:.0f} ({pnl/sell_cost:.2%}), 剩余{pos['shares']}股"
        )
        return {"pnl": pnl, "pnl_pct": pnl / sell_cost, "remaining_shares": pos["shares"]}

    def update_positions(self, prices: dict[str, float]):
        for sym, pos in self.positions.items():
            price = prices.get(sym)
            if price is None:
                continue
            pos["market_value"] = pos["shares"] * price
            pos["pnl"] = pos["market_value"] - pos["cost"]
            pos["pnl_pct"] = pos["pnl"] / pos["cost"]
            if price > pos["highest_price"]:
                pos["highest_price"] = price

    def daily_report(self, date: str) -> dict:
        total_value = self.total_capital
        self.peak_capital = max(self.peak_capital, total_value)
        dd = (total_value - self.peak_capital) / self.peak_capital if self.peak_capital > 0 else 0.0

        report = {
            "date": date,
            "total_value": round(total_value, 2),
            "cash": round(self.cash, 2),
            "exposure": round(self.total_exposure, 2),
            "position_count": self.position_count,
            "daily_pnl": 0.0,
            "drawdown": round(dd, 4),
        }
        self.daily_values.append(report)
        return report

    @property
    def portfolio_drawdown_pct(self) -> float:
        """从峰值回撤比例，负值表示亏损。"""
        if self.peak_capital <= 0:
            return 0.0
        return (self.total_capital - self.peak_capital) / self.peak_capital

    def is_portfolio_drawdown_triggered(self) -> bool:
        """组合回撤是否触发强制减仓线（总净值从峰值回撤超出限制）。"""
        return self.portfolio_drawdown_pct <= CFG.RISK.portfolio_drawdown_limit

    def check_max_loss(self) -> bool:
        if self.peak_capital <= 0:
            return False
        dd = (self.total_capital - self.peak_capital) / self.peak_capital
        return dd <= CFG.RISK.max_total_loss
