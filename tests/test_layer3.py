"""第三层门卫 + 形态识别测试"""

import numpy as np
import pandas as pd


def _make_bull_daily(n: int = 120) -> pd.DataFrame:
    """牛市日线：加速上涨"""
    close = pd.Series([10.0 + i * 0.04 + (i * i) * 0.0002 for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.05, "close": close,
        "high": close + 0.1, "low": close - 0.1,
        "volume": pd.Series([1e7 + i * 1e5 for i in range(n)]),
        "amount": pd.Series([1e9 + i * 1e6 for i in range(n)]),
    })


def _make_bear_daily(n: int = 120) -> pd.DataFrame:
    """熊市日线：持续下跌"""
    close = pd.Series([30.0 - i * 0.07 for i in range(n)])
    return pd.DataFrame({
        "open": close + 0.05, "close": close,
        "high": close + 0.1, "low": close - 0.1,
        "volume": pd.Series([1e7] * n),
        "amount": pd.Series([1e9] * n),
    })


def _make_weekly_bear(n: int = 30) -> pd.DataFrame:
    """熊市周线：持续下跌，DIF<DEA"""
    close = pd.Series([30.0 - i * 0.3 for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.1, "close": close,
        "high": close + 0.3, "low": close - 0.3,
        "volume": pd.Series([5e8] * n),
        "amount": pd.Series([1e10] * n),
    })


def _make_weekly_bull(n: int = 30) -> pd.DataFrame:
    close = pd.Series([10.0 + i * 0.15 + (i * i) * 0.003 for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.1, "close": close,
        "high": close + 0.3, "low": close - 0.3,
        "volume": pd.Series([5e8] * n),
        "amount": pd.Series([1e10] * n),
    })


def _make_monthly_bull(n: int = 12) -> pd.DataFrame:
    close = pd.Series([10.0 + i * 0.5 for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.3, "close": close,
        "high": close + 1.0, "low": close - 1.0,
        "volume": pd.Series([2e10] * n),
        "amount": pd.Series([2e11] * n),
    })


def _make_monthly_bear(n: int = 12) -> pd.DataFrame:
    """熊市月线：持续下跌"""
    close = pd.Series([30.0 - i * 0.8 for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.3, "close": close,
        "high": close + 1.0, "low": close - 1.0,
        "volume": pd.Series([2e10] * n),
        "amount": pd.Series([2e11] * n),
    })


class TestLayer3:
    def test_assess_stock_strong(self):
        from gate.layer3_stock import assess_stock
        daily = _make_bull_daily(120)
        weekly = _make_weekly_bull(30)
        monthly = _make_monthly_bull(12)
        verdict = assess_stock("000001", daily, weekly_df=weekly, monthly_df=monthly)
        assert verdict.passed_gate
        assert verdict.classification in ("强势", "一般")

    def test_assess_stock_bear(self):
        from gate.layer3_stock import assess_stock
        daily = _make_bear_daily(120)
        weekly = _make_weekly_bear(30)
        monthly = _make_monthly_bear(12)
        # 熊市: bearish_score≥1, bullish不足 → risk_warning
        verdict = assess_stock("000001", daily, weekly_df=weekly, monthly_df=monthly)
        assert verdict.bearish_score >= 1
        assert verdict.bullish_score < 2

    def test_assess_stock_with_fund_discount(self):
        from gate.layer3_stock import assess_stock
        daily = _make_bull_daily(120)
        weekly = _make_weekly_bull(30)
        monthly = _make_monthly_bull(12)
        fund = {}  # 缺失 → L4, x0.8
        verdict = assess_stock("000001", daily, weekly_df=weekly, monthly_df=monthly, fund_data=fund)
        if verdict.passed_gate:
            assert verdict.fund_confidence <= 0.85
            assert verdict.classification in ("强势", "一般")

    def test_check_exit_no_signal(self):
        from gate.layer3_stock import check_exit_signals
        daily = _make_bull_daily(120)
        result = check_exit_signals("000001", daily)
        assert not result["triggered"]
        assert result["action"] == "持仓"

    def test_stock_verdict_fields(self):
        from gate.layer3_stock import StockVerdict
        v = StockVerdict(
            symbol="000001", classification="强势", passed_gate=True,
            score=85.0, chan_buy_point="三买", chan_buy_score=18.0,
        )
        assert v.classification == "强势"
        assert v.score == 85.0
        assert v.chan_buy_point == "三买"


class TestPattern:
    def test_detect_platform_breakout(self):
        from factors.pattern import detect_platform_breakout
        n = 100
        close_vals = [10.0] * 60
        for i in range(40):
            close_vals.append(10.0 + i * 0.005)  # slow grind
        close_vals[-1] = close_vals[-2] * 1.05  # breakout on last bar
        df = pd.DataFrame({
            "open": [c - 0.02 for c in close_vals],
            "close": close_vals,
            "high": [c + 0.05 for c in close_vals],
            "low": [c - 0.05 for c in close_vals],
            "volume": [1e7] * 99 + [5e7],  # volume spike on last
        })
        result = detect_platform_breakout(df)
        assert result.iloc[-1]

    def test_detect_ma_convergence(self):
        from factors.pattern import detect_ma_convergence
        # 均线粘合：价格窄幅横盘
        n = 80
        close = [10.0 + np.sin(i) * 0.1 for i in range(n)]
        df = pd.DataFrame({
            "open": [c - 0.02 for c in close],
            "close": close,
            "high": [c + 0.05 for c in close],
            "low": [c - 0.05 for c in close],
            "volume": [1e7] * n,
        })
        result = detect_ma_convergence(df, ma_list=[5, 10, 20])
        assert result.any()  # should have some convergence

    def test_score_breakout_patterns(self):
        from factors.pattern import score_breakout_patterns
        n = 120
        close = pd.Series([10.0 + i * 0.03 + np.sin(i/10) * 0.2 for i in range(n)])
        df = pd.DataFrame({
            "open": close - 0.05, "close": close,
            "high": close + 0.1, "low": close - 0.1,
            "volume": pd.Series([1e7 + i * 1e5 for i in range(n)]),
        })
        scores = score_breakout_patterns(df)
        assert "平台突破" in scores
        assert "均线粘合" in scores
        assert "W底" in scores
        assert all(0 <= v <= 1 for v in scores.values())
