"""L4 退出链 — 两层止损 + 辅助退出（2026-06-06 修订）

不再支持打字机归因输出 stop_loss/trailing_stop（固定规则）。

未盈利阶段: 日内最低价 ≤ -2% → 初始止损
脱离成本后: 从最高点回撤 -3% → 动态止盈
辅助退出:   跌破MA20 / 超时10天
"""

import sniper.config as CFG
from sniper.data_router import DataRouter
from core.logger import get_logger

logger = get_logger("sniper.layers.l4_exit")


class ExitChain:
    """退出链。对每笔持仓逐级检查退出条件。"""

    def __init__(self, router: DataRouter | None = None):
        self.router = router or DataRouter()

    def evaluate(self, symbol: str, entry_date: str, current_date: str,
                 entry_price: float, highest_price: float) -> dict | None:
        """评估退出条件。返回退出信号 dict 或 None（继续持有）。

        Args:
            symbol: 股票代码
            entry_date: 入场日期
            current_date: 当前评估日期
            entry_price: 入场价格
            highest_price: 持仓期间最高价
        """
        bars = self.router.get_daily_bars(symbol, end=current_date)
        if bars.empty:
            return None

        recent = bars[bars["date"] <= current_date]
        if recent.empty:
            return None

        latest = recent.iloc[-1]
        close = latest.get("close", 0)
        low = latest.get("low", close)
        if close == 0:
            return None

        pnl_pct = (close - entry_price) / entry_price

        # ── Layer 1: 未盈利阶段止损 ──
        # 股价从未有效脱离成本区间（最高价 ≈ 入场价）
        # 用日内最低价判断，更贴近实盘
        if highest_price <= entry_price * 1.005:
            low_pnl = (low - entry_price) / entry_price
            if low_pnl <= CFG.EXIT.stop_loss:
                exit_price = max(entry_price * (1 + CFG.EXIT.stop_loss), close)
                pnl_at_exit = (exit_price - entry_price) / entry_price
                return {
                    "symbol": symbol, "exit": True, "reason": "初始止损",
                    "exit_price": exit_price, "pnl_pct": round(pnl_at_exit, 4),
                }

        # ── Layer 2: 脱离成本后动态止盈 ──
        # 从持仓最高点回撤到指定幅度即退出，保护浮盈
        if highest_price > entry_price:
            drawdown = (close - highest_price) / highest_price
            if drawdown <= CFG.EXIT.trailing_stop:
                exit_price = max(highest_price * (1 + CFG.EXIT.trailing_stop), close)
                return {
                    "symbol": symbol, "exit": True, "reason": "动态止盈",
                    "exit_price": exit_price,
                    "pnl_pct": round((exit_price - entry_price) / entry_price, 4),
                }

        # ── 辅助退出（所有阶段都检查）──
        # 4. 跌破 MA20
        if len(recent) >= CFG.EXIT.ma_break_below:
            ma = recent["close"].rolling(CFG.EXIT.ma_break_below).mean().iloc[-1]
            if close < ma:
                return {
                    "symbol": symbol, "exit": True, "reason": "跌破MA20",
                    "pnl_pct": round(pnl_pct, 4),
                }

        # 5. 超时退出
        if entry_date and current_date:
            import datetime
            try:
                ed = datetime.datetime.strptime(entry_date, "%Y-%m-%d")
                cd = datetime.datetime.strptime(current_date, "%Y-%m-%d")
                hold_days = (cd - ed).days
                if hold_days >= CFG.EXIT.max_hold_days:
                    return {
                        "symbol": symbol, "exit": True, "reason": "超时退出",
                        "pnl_pct": round(pnl_pct, 4),
                    }
            except ValueError:
                pass

        return None  # 继续持有
