"""L3 入场条件 — 双条件过滤（硬性 + 柔性）"""

import sniper.config as CFG
from sniper.data_router import DataRouter
from core.logger import get_logger

logger = get_logger("sniper.layers.l3_entry")


def _limit_up_threshold(symbol: str) -> float:
    """根据股票代码判断涨停阈值。

    主板 10%，创业板/科创板 20%，北交所 30%。
    """
    if symbol.startswith("30"):   # 创业板
        return 0.20
    if symbol.startswith("688"):  # 科创板
        return 0.20
    if symbol.startswith("8"):    # 北交所
        return 0.30
    return 0.10  # 主板（60/00/00开头）


class EntryFilter:
    """入场过滤器。对候选股票执行硬性条件 + 柔性条件双重过滤。"""

    def __init__(self, router: DataRouter | None = None):
        self.router = router or DataRouter()

    def check_hard(self, symbol: str, date: str) -> tuple[bool, str]:
        """硬性条件检查：一票否决。返回 (通过, 原因)。"""
        bars = self.router.get_daily_bars(symbol, end=date)
        if bars.empty:
            return False, "无行情数据"

        recent = bars[bars["date"] <= date]
        if recent.empty:
            return False, "无数据"

        latest = recent.iloc[-1]
        close = latest.get("close", 0)

        if close < CFG.ENTRY.hard_min_price:
            return False, f"股价 {close:.2f} < 最低 {CFG.ENTRY.hard_min_price}"
        if close > CFG.ENTRY.hard_max_price:
            return False, f"股价 {close:.2f} > 最高 {CFG.ENTRY.hard_max_price}"

        volume = latest.get("amount", 0) or 0
        if volume < CFG.ENTRY.hard_min_volume:
            return False, f"成交额 {volume:.0f} < 最低 {CFG.ENTRY.hard_min_volume:.0f}"

        turnover = latest.get("turnover", 0) or 0
        if turnover > CFG.ENTRY.hard_max_turnover:
            return False, f"换手率 {turnover:.2%} > 最高 {CFG.ENTRY.hard_max_turnover:.0%}"

        # 非涨停检查（区分板块：主板10%、创业板/科创板20%、北交所30%）
        if CFG.ENTRY.hard_not_limit_up:
            prev = recent.iloc[-2] if len(recent) >= 2 else None
            if prev is not None:
                pct = (close - prev["close"]) / prev["close"]
                limit = _limit_up_threshold(symbol)
                if pct >= limit - 0.005:  # 留 0.5% 容忍
                    return False, f"涨停 {pct:.1%}(阈值{limit:.0%})"

        return True, "通过"

    def check_soft(self, symbol: str, date: str, l2_score: float, sector_rank: int) -> tuple[bool, str]:
        """柔性条件检查。返回 (通过, 原因)。"""
        if l2_score < CFG.ENTRY.soft_min_score:
            return False, f"L2评分 {l2_score:.1f} < {CFG.ENTRY.soft_min_score}"
        if sector_rank > CFG.ENTRY.soft_sector_top:
            return False, f"板块排名 {sector_rank} > {CFG.ENTRY.soft_sector_top}"
        return True, "通过"

    def filter(self, candidate: dict, date: str, sector_rank: int) -> dict:
        """综合过滤候选股票。返回带决策信息的 dict。"""
        symbol = candidate["symbol"]
        l2_score = candidate.get("score", 0)

        passed_hard, reason_hard = self.check_hard(symbol, date)
        if not passed_hard:
            return {"symbol": symbol, "entry": False, "reason": reason_hard}

        passed_soft, reason_soft = self.check_soft(symbol, date, l2_score, sector_rank)
        if not passed_soft:
            return {"symbol": symbol, "entry": False, "reason": reason_soft}

        return {"symbol": symbol, "entry": True, "reason": "双条件通过", "score": l2_score}
