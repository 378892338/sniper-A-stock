"""市场状态枚举 — 单一真相源，统一中文/英文状态映射"""

from dataclasses import dataclass
from enum import Enum


class MarketState(str, Enum):
    BULL = "bull"
    VOLATILE = "volatile"
    WEAK = "weak"
    BEAR = "bear"

    @property
    def chinese(self) -> str:
        _map = {
            MarketState.BULL: "牛市",
            MarketState.VOLATILE: "震荡",
            MarketState.WEAK: "偏弱",
            MarketState.BEAR: "熊市",
        }
        return _map[self]

    @classmethod
    def from_chinese(cls, s: str) -> "MarketState":
        _map = {
            "牛市": cls.BULL,
            "震荡": cls.VOLATILE,
            "偏弱": cls.WEAK,
            "熊市": cls.BEAR,
        }
        if s not in _map:
            raise ValueError(f"未知市场状态: {s!r}，可选: {list(_map)}")
        return _map[s]

    @classmethod
    def normalize(cls, s: str | None) -> "MarketState":
        """将中英文混合输入统一为 MarketState"""
        if s is None:
            return cls.VOLATILE
        if s in ("bull", "volatile", "weak", "bear"):
            return cls(s)
        return cls.from_chinese(s)


# ── L2 TopK 联动 L1 市场状态 (§15) ──
_TOP_K_RATIO: dict[MarketState, float] = {
    MarketState.BULL: 0.45,
    MarketState.VOLATILE: 0.35,
    MarketState.WEAK: 0.25,
    MarketState.BEAR: 0.0,
}

_TOP_K_MIN_K: dict[MarketState, int] = {
    MarketState.BULL: 4,
    MarketState.VOLATILE: 3,
    MarketState.WEAK: 3,
    MarketState.BEAR: 0,
}

# 动态看空拦截阈值：偏弱市场容忍度更高，牛市更警惕反转
BEARISH_THRESHOLD_MAP: dict[MarketState, int] = {
    MarketState.BULL: 3,
    MarketState.VOLATILE: 4,
    MarketState.WEAK: 5,
    MarketState.BEAR: 999,
}

# 动态 Gate bullish 门槛：偏弱市场放宽，牛市收紧
GATE_THRESHOLD_MAP: dict[MarketState, int] = {
    MarketState.BULL: 3,
    MarketState.VOLATILE: 2,
    MarketState.WEAK: 1,
    MarketState.BEAR: 999,
}


def get_top_k_ratio(state: MarketState) -> float:
    return _TOP_K_RATIO.get(state, 0.25)


def get_top_k_min_k(state: MarketState) -> int:
    return _TOP_K_MIN_K.get(state, 3)


def get_gate_threshold(state: MarketState) -> int:
    return GATE_THRESHOLD_MAP.get(state, 2)


def get_bearish_threshold(state: MarketState) -> int:
    return BEARISH_THRESHOLD_MAP.get(state, 4)


@dataclass(frozen=True)
class PositionFormula:
    """仓位计算公式的各因子"""
    base: float
    min_confidence: float
    risk_discount: float = 0.7
    fund_discount: float = 1.0
    bearish_override: float = 1.0

    @property
    def final(self) -> float:
        return self.base * self.min_confidence * self.risk_discount * self.fund_discount * self.bearish_override
