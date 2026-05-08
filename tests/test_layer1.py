import numpy as np
import pandas as pd


def _make_weekly_bull(n: int = 100) -> pd.DataFrame:
    close = pd.Series([10.0 + i * 0.3 + np.sin(i/5) for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.2,
        "close": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "volume": pd.Series([1e8] * n),
        "amount": pd.Series([1e10] * n),
    })


def _make_weekly_bear(n: int = 100) -> pd.DataFrame:
    """Bear market: sharp drop in last 15 weeks triggers recent death cross"""
    close = pd.Series([
        50.0 - i * 0.15 + np.sin(i/5) * 0.2 if i < n - 15
        else 30.0 - (i - (n - 15)) * 1.8
        for i in range(n)
    ])
    return pd.DataFrame({
        "open": close + 0.2,
        "close": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "volume": pd.Series([1e8] * n),
        "amount": pd.Series([1e10] * n),
    })


def _make_weekly_mild_bear(n: int = 100) -> pd.DataFrame:
    """Mild bear: slow decline, only 1 bearish condition (risk_warning test)"""
    close = pd.Series([30.0 - i * 0.1 + np.sin(i/3) * 1.5 for i in range(n)])
    return pd.DataFrame({
        "open": close + 0.2,
        "close": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "volume": pd.Series([1e8] * n),
        "amount": pd.Series([1e10] * n),
    })


def _make_weekly_single_bearish(n: int = 100) -> pd.DataFrame:
    """单一看空信号: 末端急跌触发周线死叉(tail(8))，但12周跌幅-2.7%不触发阴跌"""
    close_vals = []
    for i in range(n):
        if i < 92:
            val = 10.0 + i * 0.15
        elif i < 96:
            val = 23.8 + (i - 92) * 0.03
        else:
            val = 23.92 - (i - 96) * 0.5
        close_vals.append(val)
    close = pd.Series(close_vals)
    return pd.DataFrame({
        "open": close + 0.2,
        "close": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "volume": pd.Series([1e8] * n),
        "amount": pd.Series([1e10] * n),
    })


def _make_monthly_bull(n: int = 30) -> pd.DataFrame:
    close = pd.Series([10.0 + i * 1.2 + np.sin(i/2) for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.5,
        "close": close,
        "high": close + 1.5,
        "low": close - 1.5,
        "volume": pd.Series([4e9] * n),
        "amount": pd.Series([4e11] * n),
    })


def _make_monthly_bear(n: int = 30) -> pd.DataFrame:
    """Bear monthly: sharp drop in last 6 months"""
    close = pd.Series([
        50.0 - i * 0.3 + np.sin(i/3) * 0.5 if i < n - 6
        else 35.0 - (i - (n - 6)) * 2.5
        for i in range(n)
    ])
    return pd.DataFrame({
        "open": close + 0.5,
        "close": close,
        "high": close + 1.5,
        "low": close - 1.5,
        "volume": pd.Series([4e9] * n),
        "amount": pd.Series([4e11] * n),
    })


class TestLayer1:
    def test_assess_single_bull_market(self):
        from gate.layer1_market import assess_single_market
        weekly = _make_weekly_bull(100)
        monthly = _make_monthly_bull(30)
        verdict = assess_single_market("shanghai", weekly, monthly_df=monthly)
        assert verdict.tech_pass
        assert verdict.is_strong

    def test_assess_single_bear_risk_warning(self):
        """单一看空信号触发 risk_warning（周线死叉，无阴跌）"""
        from gate.layer1_market import assess_single_market
        weekly = _make_weekly_single_bearish(100)
        verdict = assess_single_market("shanghai", weekly)
        assert verdict.risk_warning
        assert verdict.bearish_score >= 1

    def test_assess_market_all_bear_risk_warning(self):
        """三市均存在看空信号: 全部 risk_warning 导致仓位折扣"""
        from gate.layer1_market import assess_market
        data = {
            "shanghai": _make_weekly_single_bearish(100),
            "shenzhen": _make_weekly_single_bearish(100),
            "chinext": _make_weekly_single_bearish(100),
        }
        # 提供基础资金面确保 fund_pass=True（仅测试 risk_warning 折扣逻辑）
        fund_data = {
            "shanghai": {"northbound_available": True, "northbound_net_flow": 100,
                         "turnover_available": True, "turnover_trend_up": True},
            "shenzhen": {"northbound_available": True, "northbound_net_flow": 100,
                         "turnover_available": True, "turnover_trend_up": True},
            "chinext": {"northbound_available": True, "northbound_net_flow": 100,
                         "turnover_available": True, "turnover_trend_up": True},
        }
        result = assess_market(data, fund_data=fund_data)
        assert result.risk_warning
        assert result.actual_position_pct < result.base_position_pct

    def test_fund_confidence_adjustment(self):
        from gate.layer1_market import assess_market
        data = {
            "shanghai": _make_weekly_bull(100),
            "shenzhen": _make_weekly_bull(100),
            "chinext": _make_weekly_bull(100),
        }
        monthly = {
            "shanghai": _make_monthly_bull(30),
            "shenzhen": _make_monthly_bull(30),
            "chinext": _make_monthly_bull(30),
        }
        fund_data = {
            "shanghai": {"northbound_available": True, "northbound_net_flow": 100,
                         "turnover_available": True, "turnover_trend_up": True},
            "shenzhen": {"northbound_available": True, "northbound_net_flow": 100,
                         "turnover_available": True, "turnover_trend_up": True},
            "chinext": {},
        }
        result = assess_market(data, fund_data=fund_data, monthly_data=monthly)
        assert abs(result.actual_position_pct - result.base_position_pct * 0.80) < 0.01

    def test_fund_reverse_blocks(self):
        from gate.layer1_market import assess_single_market
        weekly = _make_weekly_bull(100)
        monthly = _make_monthly_bull(30)
        fund = {"northbound_available": True, "northbound_net_flow": -1000, "turnover_available": False}
        verdict = assess_single_market("shanghai", weekly, monthly_df=monthly, fund_data=fund)
        assert verdict.tech_pass
        assert not verdict.fund_pass
        assert not verdict.is_strong

    def test_daily_alert_no_trigger(self):
        from gate.layer1_market import daily_alert_check
        daily_data = {
            "shanghai": _make_weekly_bull(100),
            "shenzhen": _make_weekly_bull(100),
            "chinext": _make_weekly_bull(100),
        }
        result = daily_alert_check(daily_data)
        assert not result["triggered"]

    def test_layer1_result_fields(self):
        from gate.layer1_market import Layer1Result, MarketVerdict
        v = MarketVerdict(name="shanghai", is_strong=True, tech_pass=True,
                          fund_confidence=1.0, fund_pass=True)
        r = Layer1Result(
            verdicts={"shanghai": v},
            strong_count=1,
            market_state="偏弱",
            base_position_pct=0.30,
            actual_position_pct=0.24,
            passed=False,
        )
        assert r.base_position_pct == 0.30
        assert r.actual_position_pct == 0.24

    def test_bullish_score_counting(self):
        """Verify bullish_score is reported on MarketVerdict"""
        from gate.layer1_market import assess_single_market
        weekly = _make_weekly_bull(100)
        monthly = _make_monthly_bull(30)
        verdict = assess_single_market("shanghai", weekly, monthly_df=monthly)
        assert hasattr(verdict, 'bullish_score')
        assert hasattr(verdict, 'bearish_score')
        assert hasattr(verdict, 'risk_warning')

    def test_risk_warning_discount(self):
        """Risk warning should reduce position"""
        from gate.layer1_market import assess_market
        data = {
            "shanghai": _make_weekly_single_bearish(100),
            "shenzhen": _make_weekly_single_bearish(100),
            "chinext": _make_weekly_single_bearish(100),
        }
        # 提供基础资金面确保 fund_pass=True
        fund_data = {
            "shanghai": {"northbound_available": True, "northbound_net_flow": 100,
                         "turnover_available": True, "turnover_trend_up": True},
            "shenzhen": {"northbound_available": True, "northbound_net_flow": 100,
                         "turnover_available": True, "turnover_trend_up": True},
            "chinext": {"northbound_available": True, "northbound_net_flow": 100,
                         "turnover_available": True, "turnover_trend_up": True},
        }
        result = assess_market(data, fund_data=fund_data)
        assert result.risk_warning
        assert result.actual_position_pct < result.base_position_pct
