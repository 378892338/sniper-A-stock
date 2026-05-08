"""回测引擎单元测试"""

import pandas as pd
import numpy as np
import pytest

from backtest.run import (
    generate_rebalance_dates, calc_metrics,
    BacktestEngine, TRANSACTION_COST,
)


class TestRebalanceDates:
    def test_monthly(self):
        dates = generate_rebalance_dates("2024-01-01", "2024-12-31", "monthly")
        assert len(dates) == 12
        assert dates[0].startswith("2024-01")
        assert dates[-1].startswith("2024-12")

    def test_weekly(self):
        dates = generate_rebalance_dates("2024-01-01", "2024-01-31", "weekly")
        assert len(dates) >= 3  # 至少3个周五
        for d in dates:
            assert pd.Timestamp(d).dayofweek == 4  # 周五

    def test_daily(self):
        dates = generate_rebalance_dates("2024-01-01", "2024-01-15", "daily")
        assert len(dates) > 5  # 应该有多个交易日


class TestCalcMetrics:
    def test_positive_returns(self):
        """正收益序列的指标计算"""
        returns = pd.Series([0.01, 0.02, 0.005, 0.015, 0.01])
        m = calc_metrics(returns)
        assert m["total_return"] > 0
        assert m["annual_return"] > 0
        assert m["sharpe_ratio"] > 0
        assert m["win_rate"] > 50
        assert "max_drawdown" in m

    def test_flat_returns(self):
        """零收益序列"""
        returns = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0])
        m = calc_metrics(returns)
        assert m["total_return"] == 0.0
        assert m["sharpe_ratio"] == 0.0
        assert m["win_rate"] == 0.0

    def test_negative_returns(self):
        """负收益序列"""
        returns = pd.Series([-0.01, -0.02, -0.01])
        m = calc_metrics(returns)
        assert m["total_return"] < 0
        assert m["max_drawdown"] < 0

    def test_empty_returns(self):
        """空序列"""
        returns = pd.Series([], dtype=float)
        m = calc_metrics(returns)
        assert m == {}

    def test_with_benchmark(self):
        """含基准的指标计算（需>60个数据点才能计算alpha/beta）"""
        returns = pd.Series(np.random.randn(100) * 0.01 + 0.001)
        benchmark = pd.Series(np.random.randn(100) * 0.01 + 0.0005)
        m = calc_metrics(returns, benchmark)
        assert m["alpha"] is not None
        assert m["beta"] is not None


class TestBacktestEngine:
    def test_init_defaults(self):
        engine = BacktestEngine()
        assert engine.top_n == 30
        assert engine.freq == "monthly"
        assert engine.transaction_cost == TRANSACTION_COST

    def test_init_custom(self):
        engine = BacktestEngine(top_n=10, transaction_cost=0.001)
        assert engine.top_n == 10
        assert engine.transaction_cost == 0.001

    def test_run_with_fake_data(self):
        """用合成数据验证回测流程不报错"""
        np.random.seed(42)
        n = 300
        dates = pd.date_range("2020-01-01", periods=n, freq="B")
        daily_data = {}
        for sym in ["600001", "600002", "600003", "600004", "600005",
                     "000001", "000002", "000003", "000004", "000005",
                     "300001", "300002", "300003", "300004", "300005"]:
            close = 10 + np.cumsum(np.random.randn(n) * 0.1)
            df = pd.DataFrame({
                "open": close * (1 - np.abs(np.random.randn(n)) * 0.01),
                "high": close * (1 + np.abs(np.random.randn(n)) * 0.02),
                "low": close * (1 - np.abs(np.random.randn(n)) * 0.02),
                "close": close,
                "volume": np.random.randint(100000, 1000000, n).astype(float),
                "turnover": np.random.uniform(0.01, 0.05, n),
            }, index=dates)
            daily_data[sym] = df

        engine = BacktestEngine(
            start="2020-06-01", end="2021-01-01",
            freq="monthly", top_n=5,
            transaction_cost=0.0015,
        )
        result = engine.run(daily_data)
        assert "metrics" in result
        assert "portfolio_returns" in result
        assert "holdings_log" in result
        assert result["metrics"]["rebalance_count"] > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
