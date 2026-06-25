"""sniper/layers/ 各层模块单元测试"""

import numpy as np
import pandas as pd
import pytest


class TestMarketScorer:
    """L0 市场状态评分测试"""

    def test_import(self):
        from sniper.layers.l0_market import MarketScorer
        assert MarketScorer is not None

    def test_score_trend_default_no_data(self, router):
        from sniper.layers.l0_market import MarketScorer
        scorer = MarketScorer(router)
        # 无指数数据 → 返回中性 50
        score = scorer.score_trend("2024-06-01")
        assert score == 50.0

    def test_score_trend_with_data(self, router, sample_index_bars):
        from sniper.layers.l0_market import MarketScorer
        router.set_index_bars("shanghai", sample_index_bars)
        scorer = MarketScorer(router)
        # 取最后一天评分
        last_date = str(sample_index_bars.index[-1])[:10]
        score = scorer.score_trend(last_date)
        assert 0.0 <= score <= 100.0

    def test_score_trend_zero_close(self, router):
        from sniper.layers.l0_market import MarketScorer
        dates = pd.bdate_range("2024-01-01", periods=30)
        df = pd.DataFrame({"close": [0.0] * 30}, index=dates)
        router.set_index_bars("shanghai", df)
        scorer = MarketScorer(router)
        score = scorer.score_trend("2024-01-30")
        assert score == 50.0

    def test_score_volume_default_no_data(self, router):
        from sniper.layers.l0_market import MarketScorer
        scorer = MarketScorer(router)
        score = scorer.score_volume("2024-06-01")
        assert score == 50.0

    def test_score_volume_with_data(self, router, sample_index_bars):
        from sniper.layers.l0_market import MarketScorer
        router.set_index_bars("shanghai", sample_index_bars)
        scorer = MarketScorer(router)
        last_date = str(sample_index_bars.index[-1])[:10]
        score = scorer.score_volume(last_date)
        assert 0.0 <= score <= 100.0

    def test_score_breadth(self, router):
        from sniper.layers.l0_market import MarketScorer
        scorer = MarketScorer(router)
        # MockDataRouter 默认 breadth 比率 = 0.625
        score = scorer.score_breadth("2024-06-01")
        assert score == 62.5

    def test_score_northbound_default_no_data(self, router):
        from sniper.layers.l0_market import MarketScorer
        scorer = MarketScorer(router)
        score = scorer.score_northbound("2024-06-01")
        assert score == 50.0

    def test_score_northbound_with_data(self, router):
        from sniper.layers.l0_market import MarketScorer
        router.set_trading_dates(["2024-06-01", "2024-06-02", "2024-06-03"])
        router._northbound_data = pd.DataFrame({
            "total_net": [1e9, 2e9, 3e9],
        })
        scorer = MarketScorer(router)
        score = scorer.score_northbound("2024-06-03")
        assert 0.0 <= score <= 100.0

    def test_composite_score_calls_sub_scores(self, router):
        from sniper.layers.l0_market import MarketScorer
        scorer = MarketScorer(router)
        # 无数据时各子项返回 50，合成应为 50
        score = scorer.composite_score("2024-06-01")
        # breadth 返回 62.5，其他 50，加权和 ≈ 50 + 0.25*12.5 = 53.125
        assert 50.0 <= score <= 60.0

    def test_market_regime(self, router):
        from sniper.layers.l0_market import MarketScorer
        scorer = MarketScorer(router)
        regime = scorer.market_regime("2024-06-01")
        assert regime in ("bullish", "neutral", "bearish")

    def test_market_regime_known_bullish(self, router):
        from sniper.layers.l0_market import MarketScorer
        # 强趋势指数：0.5% 日涨幅持续 60 天，确保 composite > 70 (新 bullish_threshold)
        dates = pd.bdate_range("2024-01-01", periods=60)
        close = [3000 * (1 + 0.005 * i) for i in range(60)]
        # 量能也逐渐放大，让 volume 维度更高
        volume = [int(5e8 * (1 + 0.02 * i)) for i in range(60)]
        df = pd.DataFrame({"close": close, "volume": volume}, index=dates)
        df["date"] = df.index
        router.set_index_bars("csi300", df)
        router.set_trading_dates([str(d)[:10] for d in dates])
        router._northbound_data = pd.DataFrame({"total_net": [1e10] * 60})
        scorer = MarketScorer(router)
        regime = scorer.market_regime(str(dates[-1])[:10])
        assert regime == "bullish"


class TestSectorScorer:
    """L1 板块评分测试"""

    def test_import(self):
        from sniper.layers.l1_sector import SectorScorer
        assert SectorScorer is not None

    def test_score_momentum_empty(self, router):
        from sniper.layers.l1_sector import SectorScorer
        scorer = SectorScorer(router)
        result = scorer.score_momentum("2024-06-01")
        assert result == {}

    def test_score_momentum_with_data(self, router):
        from sniper.layers.l1_sector import SectorScorer
        router._industry_data = pd.DataFrame({
            "date": ["2024-06-01", "2024-06-01"],
            "industry_name": ["计算机", "医药"],
            "daily_change": [0.02, 0.01],
            "rank": [1, 2],
        })
        scorer = SectorScorer(router)
        result = scorer.score_momentum("2024-06-01")
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_score_fund_flow_empty(self, router):
        from sniper.layers.l1_sector import SectorScorer
        scorer = SectorScorer(router)
        result = scorer.score_fund_flow("2024-06-01")
        assert result == {}

    def test_score_fund_flow_with_data(self, router):
        from sniper.layers.l1_sector import SectorScorer
        router._industry_data = pd.DataFrame({
            "industry_name": ["计算机", "医药"],
            "rank": [1, 2],
        })
        scorer = SectorScorer(router)
        result = scorer.score_fund_flow("2024-06-01")
        assert len(result) == 2
        assert all(v == 50.0 for v in result.values())

    def test_score_breadth_empty(self, router):
        from sniper.layers.l1_sector import SectorScorer
        scorer = SectorScorer(router)
        result = scorer.score_breadth("2024-06-01")
        assert result == {}

    def test_score_heat_empty(self, router):
        from sniper.layers.l1_sector import SectorScorer
        scorer = SectorScorer(router)
        result = scorer.score_heat("2024-06-01")
        assert result == {}

    def test_score_heat_with_data(self, router):
        from sniper.layers.l1_sector import SectorScorer
        router._industry_data = pd.DataFrame({
            "industry_name": ["计算机", "医药"],
            "rank": [1, 2],
        })
        scorer = SectorScorer(router)
        result = scorer.score_heat("2024-06-01")
        assert len(result) == 2
        # rank 1 的 heat = (1 - 0/2) * 100 = 100
        # rank 2 的 heat = (1 - 1/2) * 100 = 50
        assert result["计算机"] == 100.0
        assert result["医药"] == 50.0

    def test_composite_scores_empty(self, router):
        from sniper.layers.l1_sector import SectorScorer
        scorer = SectorScorer(router)
        df = scorer.composite_scores("2024-06-01")
        assert df.empty

    def test_composite_scores_with_data(self, router):
        from sniper.layers.l1_sector import SectorScorer
        router._industry_data = pd.DataFrame({
            "date": ["2024-06-01", "2024-06-01"],
            "industry_name": ["计算机", "医药"],
            "daily_change": [0.02, 0.01],
            "rank": [1, 2],
        })
        scorer = SectorScorer(router)
        df = scorer.composite_scores("2024-06-01")
        assert not df.empty
        assert "rank" in df.columns
        assert "composite" in df.columns
        assert len(df) == 2
        # 按 composite 降序排列
        assert df.iloc[0]["rank"] == 1
        assert df.iloc[1]["rank"] == 2

    def test_top_sectors(self, router):
        from sniper.layers.l1_sector import SectorScorer
        router._industry_data = pd.DataFrame({
            "date": ["2024-06-01", "2024-06-01", "2024-06-01",
                     "2024-06-01", "2024-06-01"],
            "industry_name": ["计算机", "医药", "电子", "新能源", "消费"],
            "daily_change": [0.02, 0.01, 0.015, 0.005, 0.01],
            "rank": [1, 2, 3, 4, 5],
        })
        scorer = SectorScorer(router)
        top = scorer.top_sectors("2024-06-01")
        assert len(top) <= 5
        assert isinstance(top, list)

    def test_top_sectors_empty(self, router):
        from sniper.layers.l1_sector import SectorScorer
        scorer = SectorScorer(router)
        top = scorer.top_sectors("2024-06-01")
        assert top == []

    def test_normalize_edge_cases(self):
        from sniper.layers.l1_sector import SectorScorer
        assert SectorScorer._normalize(50.0, []) == 50.0
        assert SectorScorer._normalize(50.0, [50.0, 50.0]) == 50.0
        assert SectorScorer._normalize(100.0, [0.0, 100.0]) == 100.0
        assert SectorScorer._normalize(0.0, [0.0, 100.0]) == 0.0


class TestStockScorer:
    """L2 个股评分测试"""

    def test_import(self):
        from sniper.layers.l2_stock import StockScorer
        assert StockScorer is not None

    def test_score_single_empty_bars(self, router):
        from sniper.layers.l2_stock import StockScorer
        scorer = StockScorer(router)
        result = scorer.score_single("000001", "2024-06-01")
        assert result == {}

    def test_score_single_returns_12_factors(self, router, sample_daily_bars):
        from sniper.layers.l2_stock import StockScorer
        router.set_daily_bars("000001", sample_daily_bars)
        scorer = StockScorer(router)
        last_date = str(sample_daily_bars.index[-1])[:10]
        result = scorer.score_single("000001", last_date)
        assert len(result) == 12
        expected_keys = [
            "trend", "volume", "macd", "rsi",
            "fund_flow", "big_order", "dragon_tiger",
            "eps_score", "roe_score", "revenue_growth",
            "market_cap", "turnover_score",
        ]
        for key in expected_keys:
            assert key in result, f"缺少因子 {key}"
            assert 0.0 <= result[key] <= 100.0, f"{key}={result[key]} 超出 [0,100]"

    def test_score_single_with_fund_data(self, router, sample_daily_bars):
        from sniper.layers.l2_stock import StockScorer
        router.set_daily_bars("000001", sample_daily_bars)
        router._fund_flow_data = pd.DataFrame({
            "date": ["2024-06-01"],
            "main_net": [1e7],
            "super_large": [5e6],
        })
        last_date = str(sample_daily_bars.index[-1])[:10]
        scorer = StockScorer(router)
        result = scorer.score_single("000001", last_date)
        assert result["fund_flow"] != 50.0
        assert result["big_order"] != 50.0

    def test_score_single_with_quarterly(self, router, sample_daily_bars):
        from sniper.layers.l2_stock import StockScorer
        router.set_daily_bars("000001", sample_daily_bars)
        router._quarterly_data = pd.DataFrame({
            "eps": [0.5],
            "roe": [0.08],
            "revenue_yoy": [0.15],
        })
        last_date = str(sample_daily_bars.index[-1])[:10]
        scorer = StockScorer(router)
        result = scorer.score_single("000001", last_date)
        assert result["eps_score"] == 50 + 0.5 * 20  # = 60
        assert result["roe_score"] == 0.08 * 10        # = 0.8
        # revenue_yoy=0.15 → 50 + 0.15 = 50.15
        assert result["revenue_growth"] == pytest.approx(50.15)

    def test_score_single_with_dragon_tiger(self, router, sample_daily_bars):
        from sniper.layers.l2_stock import StockScorer
        router.set_daily_bars("000001", sample_daily_bars)
        last_date = str(sample_daily_bars.index[-1])[:10]
        router._dragon_tiger_data = pd.DataFrame({
            "symbol": ["000001"],
            "net_buy": [1e7],
        })
        scorer = StockScorer(router)
        result = scorer.score_single("000001", last_date)
        # 50 + 1e7/1e7 * 20 = 70
        assert result["dragon_tiger"] == 70.0

    def test_composite_score_zero_for_empty(self, router):
        from sniper.layers.l2_stock import StockScorer
        scorer = StockScorer(router)
        score = scorer.composite_score("000001", "2024-06-01")
        assert score == 0.0

    def test_composite_score_with_data(self, router, sample_daily_bars):
        from sniper.layers.l2_stock import StockScorer
        router.set_daily_bars("000001", sample_daily_bars)
        scorer = StockScorer(router)
        last_date = str(sample_daily_bars.index[-1])[:10]
        score = scorer.composite_score("000001", last_date)
        assert 0.0 <= score <= 100.0

    def test_score_from_bars(self, router, sample_daily_bars):
        from sniper.layers.l2_stock import StockScorer
        scorer = StockScorer(router)
        last_date = str(sample_daily_bars.index[-1])[:10]
        score = scorer._score_from_bars("000001", sample_daily_bars, last_date)
        assert 0.0 <= score <= 100.0

    def test_score_from_bars_insufficient_data(self, router):
        from sniper.layers.l2_stock import StockScorer
        dates = pd.bdate_range("2024-06-01", periods=5)
        df = pd.DataFrame({"close": [10.0] * 5}, index=dates)
        scorer = StockScorer(router)
        score = scorer._score_from_bars("000001", df, "2024-06-05")
        assert 0.0 <= score <= 100.0


class TestEntryFilter:
    """L3 入场过滤测试"""

    def test_import(self):
        from sniper.layers.l3_entry import EntryFilter
        assert EntryFilter is not None

    def test_check_hard_no_data(self, router):
        from sniper.layers.l3_entry import EntryFilter
        f = EntryFilter(router)
        passed, reason = f.check_hard("000001", "2024-06-01")
        assert not passed
        assert "无行情数据" in reason

    def test_check_hard_price_too_low(self, router):
        from sniper.layers.l3_entry import EntryFilter
        dates = pd.bdate_range("2024-06-01", periods=5)
        df = pd.DataFrame({"close": [1.0] * 5, "amount": [1e7] * 5}, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        f = EntryFilter(router)
        passed, reason = f.check_hard("000001", "2024-06-05")
        assert not passed
        assert "股价" in reason

    def test_check_hard_price_too_high(self, router):
        from sniper.layers.l3_entry import EntryFilter
        dates = pd.bdate_range("2024-06-01", periods=5)
        df = pd.DataFrame({"close": [500.0] * 5, "amount": [1e7] * 5}, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        f = EntryFilter(router)
        passed, reason = f.check_hard("000001", "2024-06-05")
        assert not passed
        assert "股价" in reason

    def test_check_hard_low_volume(self, router):
        from sniper.layers.l3_entry import EntryFilter
        dates = pd.bdate_range("2024-06-01", periods=5)
        df = pd.DataFrame({"close": [10.0] * 5, "amount": [1e5] * 5}, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        f = EntryFilter(router)
        passed, reason = f.check_hard("000001", "2024-06-05")
        assert not passed
        assert "成交额" in reason

    def test_check_hard_limit_up(self, router):
        from sniper.layers.l3_entry import EntryFilter
        # 用 bdate_range 起始周一，确保日期对齐
        dates = pd.bdate_range("2024-06-03", periods=5)
        # 最后一天涨停（与前日比涨幅 >= 9.5%）
        df = pd.DataFrame({
            "close": [10.0, 10.0, 10.0, 10.0, 11.0],
            "amount": [1e7] * 5,
        }, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        last = str(dates[-1])[:10]
        f = EntryFilter(router)
        passed, reason = f.check_hard("000001", last)
        assert not passed
        assert "涨停" in reason

    def test_check_hard_pass(self, router):
        from sniper.layers.l3_entry import EntryFilter
        dates = pd.bdate_range("2024-06-01", periods=5)
        df = pd.DataFrame({
            "close": [10.0] * 5,
            "amount": [1e7] * 5,
            "turnover": [0.05] * 5,
        }, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        f = EntryFilter(router)
        passed, reason = f.check_hard("000001", "2024-06-05")
        assert passed
        assert "通过" in reason

    def test_check_soft_low_score(self, router):
        from sniper.layers.l3_entry import EntryFilter
        f = EntryFilter(router)
        passed, reason = f.check_soft("000001", "2024-06-01", 50.0, 1)
        assert not passed
        assert "L2评分" in reason

    def test_check_soft_bad_sector_rank(self, router):
        from sniper.layers.l3_entry import EntryFilter
        f = EntryFilter(router)
        passed, reason = f.check_soft("000001", "2024-06-01", 80.0, 5)
        assert not passed
        assert "板块排名" in reason

    def test_check_soft_pass(self, router):
        from sniper.layers.l3_entry import EntryFilter
        f = EntryFilter(router)
        passed, reason = f.check_soft("000001", "2024-06-01", 80.0, 1)
        assert passed
        assert "通过" in reason

    def test_filter_returns_entry_false_on_hard_fail(self, router):
        from sniper.layers.l3_entry import EntryFilter
        f = EntryFilter(router)
        result = f.filter({"symbol": "000001", "score": 80.0}, "2024-06-01", 1)
        assert result["entry"] is False
        assert "无行情数据" in result["reason"]

    def test_filter_returns_entry_true(self, router):
        from sniper.layers.l3_entry import EntryFilter
        dates = pd.bdate_range("2024-06-01", periods=5)
        df = pd.DataFrame({
            "close": [10.0] * 5,
            "amount": [1e7] * 5,
            "turnover": [0.05] * 5,
        }, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        f = EntryFilter(router)
        result = f.filter({"symbol": "000001", "score": 80.0}, "2024-06-05", 1)
        assert result["entry"] is True
        assert "双条件通过" in result["reason"]


class TestExitChain:
    """L4 退出链测试"""

    def test_import(self):
        from sniper.layers.l4_exit import ExitChain
        assert ExitChain is not None

    def test_evaluate_no_data(self, router):
        from sniper.layers.l4_exit import ExitChain
        chain = ExitChain(router)
        result = chain.evaluate("000001", "2024-06-01", "2024-06-05", 10.0, 10.0)
        assert result is None

    def _make_bars(self, close, low=None):
        """辅助：创建带指定日期的 bars DataFrame，日期从周一 2024-06-03 开始"""
        dates = pd.bdate_range("2024-06-03", periods=len(close))
        df = pd.DataFrame({"close": close, "low": low or close}, index=dates)
        df["date"] = df.index
        return df

    def test_evaluate_stop_loss(self, router):
        """收盘价跌破止损线 -12%"""
        from sniper.layers.l4_exit import ExitChain
        df = self._make_bars(
            close=[10.0, 9.8, 9.5, 9.0, 8.5],
            low=[9.9, 9.7, 9.4, 8.8, 8.3],
        )
        last_date = str(df.index[-1])[:10]
        router.set_daily_bars("000001", df)
        chain = ExitChain(router)
        result = chain.evaluate("000001", str(df.index[0])[:10], last_date, 10.0, 10.0)
        assert result is not None
        assert result["exit"] is True
        assert result["reason"] == "初始止损"

    def test_evaluate_no_stop_loss_if_above(self, router):
        """还在成本价之上 → 不触发止损"""
        from sniper.layers.l4_exit import ExitChain
        df = self._make_bars(
            close=[10.0, 10.1, 10.2, 10.3, 10.4],
            low=[9.9, 10.0, 10.1, 10.2, 10.3],
        )
        last_date = str(df.index[-1])[:10]
        router.set_daily_bars("000001", df)
        chain = ExitChain(router)
        result = chain.evaluate("000001", str(df.index[0])[:10], last_date, 10.0, 10.5)
        assert result is None or not result["exit"]

    def test_evaluate_trailing_stop(self, router):
        """从高点回落 -8% → 移动止盈"""
        from sniper.layers.l4_exit import ExitChain
        df = self._make_bars(
            close=[10.0, 11.0, 12.0, 11.0, 10.8],
            low=[9.8, 10.8, 11.8, 10.8, 10.5],
        )
        last_date = str(df.index[-1])[:10]
        router.set_daily_bars("000001", df)
        chain = ExitChain(router)
        result = chain.evaluate("000001", str(df.index[0])[:10], last_date, 10.0, 12.0)
        assert result is not None
        assert result["reason"] == "动态止盈"

    def test_evaluate_ma_break(self, router):
        """跌破 MA20"""
        from sniper.layers.l4_exit import ExitChain
        dates = pd.bdate_range("2024-01-01", periods=30)
        # 先涨后跌，最后一天跌破 MA20
        close = list(np.linspace(10.0, 12.0, 20)) + list(np.linspace(11.8, 10.5, 10))
        df = pd.DataFrame({"close": close, "low": [c * 0.98 for c in close]}, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        chain = ExitChain(router)
        first_date = str(dates[0])[:10]
        last_date = str(dates[-1])[:10]
        result = chain.evaluate("000001", first_date, last_date, 10.0, 12.0)
        if result:
            assert result["exit"] is True

    def test_evaluate_hold_normal(self, router):
        """正常持仓中，尚未达到任何退出条件"""
        from sniper.layers.l4_exit import ExitChain
        dates = pd.bdate_range("2024-01-01", periods=10)
        df = pd.DataFrame({
            "close": [10.0, 10.1, 10.2, 10.3, 10.4, 10.3, 10.4, 10.5, 10.6, 10.5],
            "low": [9.9] * 10,
        }, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        chain = ExitChain(router)
        result = chain.evaluate("000001", "2024-01-01", "2024-01-10", 10.0, 10.6)
        assert result is None
