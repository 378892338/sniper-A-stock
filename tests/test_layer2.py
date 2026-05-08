"""第二层门卫测试"""

import numpy as np
import pandas as pd


def _make_weekly_bull(n: int = 80) -> pd.DataFrame:
    """牛市：加速上涨趋势，MACD金叉状态"""
    close = pd.Series([10.0 + i * 0.08 + (i*i)*0.001 for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.1, "close": close,
        "high": close + 0.3, "low": close - 0.3,
        "volume": pd.Series([5e8 + i*2e6 for i in range(len(close))]),
        "amount": pd.Series([1e10 + i*5e7 for i in range(len(close))]),
    })


def _make_weekly_bear(n: int = 80) -> pd.DataFrame:
    """熊市周线：持续下跌，确保DIF<DEA且柱状图为负"""
    close = pd.Series([30.0 - i * 0.25 for i in range(n)])
    return pd.DataFrame({
        "open": close + 0.1, "close": close,
        "high": close + 0.3, "low": close - 0.3,
        "volume": pd.Series([5e8 - i*1e6 for i in range(n)]),
        "amount": pd.Series([1e10 - i*1e7 for i in range(n)]),
    })


def _make_monthly_bear(n: int = 24) -> pd.DataFrame:
    """熊市月线：持续下跌"""
    close = pd.Series([30.0 - i * 1.0 for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.3, "close": close,
        "high": close + 1.0, "low": close - 1.0,
        "volume": pd.Series([2e10] * n),
        "amount": pd.Series([2e11] * n),
    })


def _make_monthly_bull(n: int = 24) -> pd.DataFrame:
    close = pd.Series([10.0 + i * 0.8 + np.sin(i/2) for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.3, "close": close,
        "high": close + 1.0, "low": close - 1.0,
        "volume": pd.Series([2e10] * n),
        "amount": pd.Series([2e11] * n),
    })


def _make_benchmark_bull(n: int = 80) -> pd.Series:
    """基准慢涨：确保ETF能跑赢"""
    return pd.Series([10.0 + i * 0.06 for i in range(n)])


class TestLayer2:
    def test_assess_single_sector_bull(self):
        from gate.layer2_sector import assess_single_sector
        weekly = _make_weekly_bull(80)
        monthly = _make_monthly_bull(24)
        bench = _make_benchmark_bull(80)
        verdict = assess_single_sector(
            "芯片", "990001", weekly, monthly_df=monthly, benchmark_close=bench
        )
        # 牛市ETF应通过
        assert verdict.passed_gate
        assert verdict.score > 0

    def test_assess_single_sector_bear(self):
        from gate.layer2_sector import assess_single_sector
        weekly = _make_weekly_bear(80)
        monthly = _make_monthly_bear(24)
        bench = _make_benchmark_bull(80)
        verdict = assess_single_sector(
            "芯片", "990001", weekly, monthly_df=monthly, benchmark_close=bench
        )
        # 熊市ETF: bullish条件不足2票, bearish_score≥1 → risk_warning
        assert verdict.bearish_score >= 1
        assert verdict.bullish_score < 2
        assert verdict.risk_warning

    def test_assess_sectors_integration(self):
        from gate.layer2_sector import assess_sectors
        etf_data = {
            "芯片": _make_weekly_bull(80),
            "银行": _make_weekly_bull(80),
            "军工": _make_weekly_bull(80),
            "煤炭": _make_weekly_bear(80),
        }
        monthly = {k: _make_monthly_bull(24) for k in etf_data}
        bench = _make_benchmark_bull(80)
        result = assess_sectors(etf_data, monthly_data=monthly, benchmark_close=bench)
        assert len(result.candidate_sectors) >= 2
        assert result.passed

    def test_no_strong_sectors(self):
        from gate.layer2_sector import assess_sectors
        etf_data = {
            "芯片": _make_weekly_bear(80),
            "银行": _make_weekly_bear(80),
            "军工": _make_weekly_bear(80),
        }
        monthly = {k: _make_monthly_bear(24) for k in etf_data}
        bench = _make_benchmark_bull(80)
        result = assess_sectors(etf_data, monthly_data=monthly, benchmark_close=bench)
        # 弱市ETF仍可能过门（bearish=1时通过），但总体会有risk_warning
        assert result.risk_warning


class TestSectorMapper:
    def test_industry_to_etf(self):
        from gate.sector_mapper import SectorMapper
        sm = SectorMapper()
        etfs = sm.map_stock_to_etf(symbol_industry="电子")
        assert "芯片" in etfs

    def test_concept_to_etf(self):
        from gate.sector_mapper import SectorMapper
        sm = SectorMapper()
        etfs = sm.map_stock_to_etf(
            symbol_industry="综合",
            symbol_concepts=["CPO", "存储芯片"],
        )
        assert "芯片" in etfs

    def test_no_mapping_fallback(self):
        from gate.sector_mapper import SectorMapper
        sm = SectorMapper()
        etfs = sm.map_stock_to_etf(symbol_industry="未知行业")
        assert etfs == []

    def test_correlation_fallback(self):
        from gate.sector_mapper import SectorMapper
        sm = SectorMapper()
        etfs = sm.map_stock_to_etf(
            symbol_industry="未知行业",
            price_corr_with_etfs={"芯片": 0.85, "银行": 0.3, "军工": 0.4},
        )
        assert "芯片" in etfs

    def test_etf_code_lookup(self):
        from gate.sector_mapper import SectorMapper
        sm = SectorMapper()
        assert sm.get_etf_code("芯片") == "990001"
        assert sm.get_etf_code("不存在的ETF") is None


class TestAlpha:
    def test_calc_alpha_positive(self):
        from factors.alpha import calc_alpha
        stock = pd.Series([10 + i * 0.2 for i in range(30)])
        bench = pd.Series([10 + i * 0.1 for i in range(30)])
        alpha = calc_alpha(stock, bench, 20)
        assert alpha.dropna().iloc[-1] > 0  # 跑赢

    def test_is_outperforming(self):
        from factors.alpha import is_outperforming
        stock = pd.Series([10 + i * 0.2 for i in range(20)])
        bench = pd.Series([10 + i * 0.1 for i in range(20)])
        assert is_outperforming(stock, bench)

    def test_is_underperforming(self):
        from factors.alpha import is_outperforming
        stock = pd.Series([10 + i * 0.05 for i in range(20)])
        bench = pd.Series([10 + i * 0.2 for i in range(20)])
        assert not is_outperforming(stock, bench)


class TestThreshold:
    def test_default_top_k(self):
        from gate.threshold import default_top_k
        assert default_top_k(10) == 3  # max(3, 10*0.3)
        assert default_top_k(20) == 6  # max(3, 20*0.3)
        assert default_top_k(3) == 3   # max(3, 1)

    def test_percentile_threshold(self):
        from gate.threshold import percentile_threshold
        values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        assert abs(percentile_threshold(values, 50) - 55) < 1

    def test_optimal_threshold(self):
        from gate.threshold import optimal_threshold_from_backtest
        thresholds = [0.1, 0.2, 0.3, 0.4, 0.5]
        win_rates = [0.4, 0.5, 0.65, 0.6, 0.55]
        drawdowns = [0.3, 0.25, 0.15, 0.2, 0.22]
        best = optimal_threshold_from_backtest(thresholds, win_rates, drawdowns)
        assert best == 0.3  # 最高胜率+最低回撤


class TestFundFallback:
    def test_determine_fund_level_l1_full(self):
        from gate.fund_fallback import determine_fund_level
        level, conf, passed, note = determine_fund_level(
            has_northbound=True, has_turnover=True,
            northbound_inflow=500, amount_trend_up=True,
            for_layer=1,
        )
        assert level == "L1"
        assert conf == 1.0
        assert passed

    def test_determine_fund_level_l4_missing(self):
        from gate.fund_fallback import determine_fund_level
        level, conf, passed, note = determine_fund_level(
            has_northbound=False, has_turnover=False,
            for_layer=1,
        )
        assert level == "L4"
        assert conf == 0.80
        assert passed  # 缺失不是否定

    def test_determine_fund_level_reverse(self):
        from gate.fund_fallback import determine_fund_level
        level, conf, passed, note = determine_fund_level(
            has_northbound=True, has_turnover=False,
            northbound_inflow=-500, for_layer=1,
        )
        assert not passed  # 资金反向不通过
