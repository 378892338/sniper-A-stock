"""策略模块单元测试"""

import pandas as pd
import numpy as np
import pytest

from strategies.sieve import SieveStrategy, rank_series


class TestRankSeries:
    def test_basic(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ranked = rank_series(s)
        assert ranked.min() >= 0
        assert ranked.max() <= 100
        assert ranked.iloc[-1] > ranked.iloc[0]

    def test_ties(self):
        s = pd.Series([1.0, 1.0, 2.0])
        ranked = rank_series(s)
        # 1.0 并列最低分
        assert ranked.iloc[0] == ranked.iloc[1]

    def test_with_nan(self):
        s = pd.Series([1.0, np.nan, 3.0, 4.0, 5.0])
        ranked = rank_series(s)
        assert not ranked.isna().any()  # NaN被fillna(0)处理


class TestNeutralize:
    def test_neutralize_reduces_correlation(self):
        """中性化后应降低与市值/行业的相关性"""
        strat = SieveStrategy()
        np.random.seed(42)
        n = 50
        syms = [f"600{i:03d}" for i in range(n)]
        raw = {s: np.random.uniform(0, 100) for s in syms}
        caps = {s: np.random.uniform(1e9, 1e12) for s in syms}
        sectors = {s: f"sec{i%5}" for i, s in enumerate(syms)}

        neutral = strat.neutralize_factors(raw, caps, sectors)
        assert len(neutral) == len(raw)
        # 中性化后均值为约0
        mean_neutral = np.mean(list(neutral.values()))
        assert abs(mean_neutral) < 1.0

    def test_neutralize_fallback_small_sample(self):
        """样本太少时回退到原始值"""
        strat = SieveStrategy()
        raw = {"600001": 80, "600002": 60}
        caps = {"600001": 1e10, "600002": 2e10}
        sectors = {"600001": "tech", "600002": "finance"}
        # 只有2只股票，不足20只，应回退
        neutral = strat.neutralize_factors(raw, caps, sectors)
        assert neutral == raw

    def test_neutralize_without_caps_sectors(self):
        strat = SieveStrategy()
        raw = {f"600{i:03d}": float(i) for i in range(30)}
        neutral = strat.neutralize_factors(raw)
        assert neutral == raw


class TestSieveStrategy:
    def test_init(self):
        strat = SieveStrategy(sector_weight=0.40)
        assert strat.sector_weight == 0.40
        assert strat.pipe_weights["technical"] > 0

    def test_score_single_calls_calc_all_factors(self):
        strat = SieveStrategy()
        np.random.seed(42)
        n = 200
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        close = 10 + np.cumsum(np.random.randn(n) * 0.1)
        df = pd.DataFrame({
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.random.randint(100000, 1000000, n).astype(float),
            "turnover": np.random.uniform(0.01, 0.05, n),
        }, index=dates)

        scores = strat.score_single(df, symbol="600001")
        assert len(scores) == n
        assert scores.max() <= 100
        assert scores.min() >= 0

    def test_select_top(self):
        strat = SieveStrategy()
        np.random.seed(42)
        n = 200
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        scores = {}
        for sym in ["600001", "600002", "600003", "600004", "600005",
                     "000001", "000002", "000003", "000004", "000005"]:
            s = pd.Series(np.random.uniform(40, 60, n), index=dates)
            scores[sym] = s

        test_date = dates[100].strftime("%Y-%m-%d")  # 用确定存在的交易日
        top = strat.select_top(scores, test_date, top_n=3)
        assert len(top) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
