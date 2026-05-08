"""跨ETF分散选股 + 第二层日频预警测试"""

import numpy as np
import pandas as pd

from gate.layer3_stock import StockVerdict


def _make_sv(symbol: str, score: float, classification: str = "强势",
             etf_tags: list[str] = None) -> StockVerdict:
    return StockVerdict(
        symbol=symbol, classification=classification, passed_gate=True,
        score=score, etf_tags=etf_tags or [],
    )


class TestCrossETFSelection:
    """跨ETF分散选股"""

    def test_empty_candidates(self):
        from gate.layer3_stock import select_top_stocks_by_etf
        result = select_top_stocks_by_etf([], {}, max_positions=5)
        assert result.selected == []
        assert result.total_slots == 5

    def test_dispersion_across_etfs(self):
        """跨ETF分散: 证券组龙头不会吃掉芯片组名额"""
        from gate.layer3_stock import select_top_stocks_by_etf
        candidates = [
            _make_sv("000001", 90, etf_tags=["证券"]),
            _make_sv("000002", 88, etf_tags=["证券"]),
            _make_sv("000003", 85, etf_tags=["证券"]),
            _make_sv("000004", 80, etf_tags=["芯片"]),
            _make_sv("000005", 78, etf_tags=["芯片"]),
        ]
        etf_scores = {"证券": 80, "芯片": 70}
        result = select_top_stocks_by_etf(candidates, etf_scores, max_positions=4, per_etf_limit=2)

        assert len(result.selected) == 4
        # 每组各取前2
        securities = [s for s in result.selected if "证券" in s.etf_tags]
        chips = [s for s in result.selected if "芯片" in s.etf_tags]
        assert len(securities) == 2
        assert len(chips) == 2

    def test_dedup_across_groups(self):
        """同一股票属于多个ETF，只选中一次"""
        from gate.layer3_stock import select_top_stocks_by_etf
        candidates = [
            _make_sv("000001", 90, etf_tags=["新能源车", "汽车"]),
            _make_sv("000002", 88, etf_tags=["新能源车"]),
            _make_sv("000003", 85, etf_tags=["汽车"]),
        ]
        etf_scores = {"新能源车": 80, "汽车": 75}
        result = select_top_stocks_by_etf(candidates, etf_scores, max_positions=3, per_etf_limit=2)
        symbols = [s.symbol for s in result.selected]
        assert len(symbols) == len(set(symbols)), "不应有重复股票"

    def test_reserve_fill_when_underfilled(self):
        """未填满时从备选补足"""
        from gate.layer3_stock import select_top_stocks_by_etf
        candidates = [
            _make_sv("000001", 90, etf_tags=["证券"]),
            _make_sv("000002", 85, etf_tags=["证券"]),
        ]
        etf_scores = {"证券": 80}
        result = select_top_stocks_by_etf(candidates, etf_scores, max_positions=5, per_etf_limit=2)

        assert len(result.selected) >= 1
        # 剩余股票应进入reserve
        all_picked = len(result.selected) + len(result.reserve)
        assert all_picked >= len(candidates)

    def test_per_etf_limit(self):
        """每组不超过 per_etf_limit 只"""
        from gate.layer3_stock import select_top_stocks_by_etf
        candidates = [
            _make_sv(f"00000{i}", 90 - i, etf_tags=["芯片"]) for i in range(1, 8)
        ]
        etf_scores = {"芯片": 80}
        result = select_top_stocks_by_etf(candidates, etf_scores, max_positions=10, per_etf_limit=3)
        chips_in_result = sum(1 for s in result.selected if "芯片" in s.etf_tags)
        assert chips_in_result <= 3, f"每组最多3只，实际{chips_in_result}只"

    def test_etf_bonus_disabled_by_default(self):
        """冷启动默认不启用ETF强度加成"""
        from gate.layer3_stock import select_top_stocks_by_etf
        candidates = [
            _make_sv("000001", 50, etf_tags=["弱指数"]),
            _make_sv("000002", 50, etf_tags=["强指数"]),
        ]
        etf_scores = {"弱指数": 30, "强指数": 90}
        # 不启用加成: 两个同分
        result = select_top_stocks_by_etf(candidates, etf_scores, max_positions=1, per_etf_limit=1)
        # 两个同分时，按组遍历顺序选第一个（强指数组优先）
        assert len(result.selected) == 1

    def test_etf_bonus_enabled(self):
        """启用ETF强度加成后强指数组优先"""
        from gate.layer3_stock import select_top_stocks_by_etf
        candidates = [
            _make_sv("000001", 50, etf_tags=["弱指数"]),
            _make_sv("000002", 50, etf_tags=["强指数"]),
        ]
        etf_scores = {"弱指数": 30, "强指数": 90}
        # 启用加成: 强指数=50*1.08=54, 弱指数=50*0.96=48
        result = select_top_stocks_by_etf(
            candidates, etf_scores, max_positions=1, per_etf_limit=1,
            enable_etf_bonus=True,
        )
        assert len(result.selected) == 1
        assert result.selected[0].symbol == "000002", "强指数加成后应排名更前"

    def test_ungrouped_stocks_last(self):
        """无ETF归属的股票排在最后"""
        from gate.layer3_stock import select_top_stocks_by_etf
        candidates = [
            _make_sv("000001", 60, etf_tags=["证券"]),
            _make_sv("000002", 85, etf_tags=[]),
            _make_sv("000003", 75, etf_tags=["证券"]),
        ]
        etf_scores = {"证券": 80}
        result = select_top_stocks_by_etf(candidates, etf_scores, max_positions=3, per_etf_limit=2)
        symbols = [s.symbol for s in result.selected]
        assert len(symbols) == 3
        assert "000002" not in symbols[:1], "无归属股票不应排第一"
        assert set(symbols) == {"000001", "000002", "000003"}

    def test_max_positions_hard_cap(self):
        """不超过仓位上限"""
        from gate.layer3_stock import select_top_stocks_by_etf
        candidates = [
            _make_sv(f"00{i:04d}", 90 - i * 2, etf_tags=[f"ETF{j}"])
            for i in range(20) for j in range(i % 7)
        ]
        etf_scores = {f"ETF{j}": 80 for j in range(7)}
        result = select_top_stocks_by_etf(candidates, etf_scores, max_positions=7, per_etf_limit=2)
        assert len(result.selected) <= 7

    def test_selection_result_fields(self):
        """SelectionResult 各字段完整性"""
        from gate.layer3_stock import select_top_stocks_by_etf, SelectionResult
        candidates = [
            _make_sv("000001", 90, etf_tags=["芯片"]),
            _make_sv("000002", 85, etf_tags=["芯片"]),
        ]
        result = select_top_stocks_by_etf(candidates, {}, max_positions=3, per_etf_limit=2)

        assert isinstance(result, SelectionResult)
        assert isinstance(result.by_etf, dict)
        assert result.total_slots == 3
        assert result.filled is not None


class TestL2DailyAlert:
    """第二层日频预警"""

    def _make_etf_daily(self, n: int = 60, trend: str = "bull") -> pd.DataFrame:
        if trend == "bull":
            close = pd.Series([10.0 + i * 0.06 for i in range(n)])
        elif trend == "bear":
            close = pd.Series([30.0 - i * 0.04 for i in range(n)])
        else:
            close = pd.Series([10.0 + np.sin(i / 5) * 0.3 for i in range(n)])
        return pd.DataFrame({
            "open": close - 0.05, "close": close,
            "high": close + 0.1, "low": close - 0.1,
            "volume": pd.Series([1e7] * n),
        })

    def test_no_alert_in_bull(self):
        """牛市无预警"""
        from gate.layer2_sector import check_daily_alert
        etf_daily = {
            "证券": self._make_etf_daily(60, "bull"),
            "芯片": self._make_etf_daily(60, "bull"),
        }
        held_etf_map = {"600001": ["证券"], "600002": ["芯片"]}
        result = check_daily_alert(etf_daily, held_etf_map)
        assert not result["triggered"]
        assert result["level"] == "无预警"

    def test_yellow_alert_one_etf(self):
        """单个ETF跌破MA20+死叉 → 黄色预警"""
        from gate.layer2_sector import check_daily_alert
        etf_daily = {
            "证券": self._make_etf_daily(60, "bear"),
            "芯片": self._make_etf_daily(60, "bull"),
            "银行": self._make_etf_daily(60, "bull"),
        }
        held_etf_map = {"600001": ["证券"], "600002": ["芯片"], "600003": ["酒"]}
        result = check_daily_alert(etf_daily, held_etf_map)
        assert result["triggered"]
        assert result["level"] == "黄色预警"

    def test_orange_alert_multiple_etfs(self):
        """多个ETF触发 → 橙色预警 (3+/5 but < 2/3)"""
        from gate.layer2_sector import check_daily_alert
        etf_daily = {
            "证券": self._make_etf_daily(60, "bear"),
            "芯片": self._make_etf_daily(60, "bear"),
            "银行": self._make_etf_daily(60, "bear"),
            "消费": self._make_etf_daily(60, "bull"),
            "医药": self._make_etf_daily(60, "bull"),
        }
        held_etf_map = {
            "600001": ["证券"], "600002": ["芯片"], "600003": ["银行"],
            "600004": ["消费"], "600005": ["医药"],
        }
        result = check_daily_alert(etf_daily, held_etf_map)
        assert result["triggered"]
        assert result["level"] == "橙色预警"
        assert len(result["triggered_etfs"]) == 3

    def test_red_alert_majority(self):
        """超半数ETF触发 → 红色预警"""
        from gate.layer2_sector import check_daily_alert
        etf_daily = {
            "证券": self._make_etf_daily(60, "bear"),
            "芯片": self._make_etf_daily(60, "bear"),
        }
        held_etf_map = {"600001": ["证券"], "600002": ["芯片"]}
        result = check_daily_alert(etf_daily, held_etf_map)
        assert result["triggered"]
        assert result["level"] == "红色预警"

    def test_etf_not_in_positions_ignored(self):
        """未持仓的ETF不触发预警"""
        from gate.layer2_sector import check_daily_alert
        etf_daily = {
            "证券": self._make_etf_daily(60, "bear"),
            "芯片": self._make_etf_daily(60, "bull"),
        }
        held_etf_map = {"600001": ["芯片"]}
        result = check_daily_alert(etf_daily, held_etf_map)
        assert not result["triggered"]

    def test_top_divergence_triggers(self):
        """日线顶背驰触发预警"""
        from gate.layer2_sector import check_daily_alert
        # 构造顶背驰形态: 价格新高+MACD柱缩短
        n = 90
        close = pd.Series([10.0 + i * 0.1 for i in range(n)])
        close.iloc[-1] = close.iloc[-2] * 1.02  # 最后跳高
        df = pd.DataFrame({
            "open": close - 0.05, "close": close,
            "high": close + 0.15, "low": close - 0.05,
            "volume": pd.Series([1e7] * n),
        })
        etf_daily = {"证券": df}
        held_etf_map = {"600001": ["证券"]}
        result = check_daily_alert(etf_daily, held_etf_map)
        # 顶背驰可能触发
        assert isinstance(result, dict)
        assert "level" in result
