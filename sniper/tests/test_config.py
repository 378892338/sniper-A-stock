"""sniper/config.py 冻结数据类测试"""


class TestMarketConfig:
    def test_default_values(self):
        from sniper.config import MarketConfig
        cfg = MarketConfig()
        assert cfg.trend_window == 20
        assert cfg.volume_window == 20
        assert cfg.bullish_threshold == 64.0
        assert cfg.bearish_threshold == 30.0

    def test_frozen(self):
        from sniper.config import MarketConfig
        cfg = MarketConfig()
        import pytest
        with pytest.raises(Exception):
            cfg.trend_window = 99

    def test_custom_values(self):
        from sniper.config import MarketConfig
        cfg = MarketConfig(trend_window=10, bullish_threshold=70.0)
        assert cfg.trend_window == 10
        assert cfg.bullish_threshold == 70.0


class TestSectorConfig:
    def test_default_values(self):
        from sniper.config import SectorConfig
        cfg = SectorConfig()
        assert cfg.momentum_window == 5
        assert cfg.top_n == 5
        assert cfg.top_n_high == 3
        assert cfg.top_n_low == 3
        assert cfg.momentum_weight == 0.35
        assert cfg.fund_flow_weight == 0.25


class TestStockConfig:
    def test_default_values(self):
        from sniper.config import StockConfig
        cfg = StockConfig()
        assert cfg.momentum_window == 10
        assert cfg.rsi_window == 14
        assert cfg.top_n == 10
        assert cfg.trend_factor_weight == 0.25
        assert cfg.market_cap_weight == 0.04

    def test_weights_sum_reasonable(self):
        from sniper.config import StockConfig
        cfg = StockConfig()
        weights = [
            cfg.trend_factor_weight, cfg.volume_factor_weight,
            cfg.macd_factor_weight, cfg.rsi_factor_weight,
            cfg.fund_flow_weight, cfg.big_order_weight,
            cfg.dragon_tiger_weight, cfg.eps_weight,
            cfg.roe_weight, cfg.revenue_growth_weight,
            cfg.market_cap_weight, cfg.turnover_weight,
        ]
        total = sum(weights)
        assert total == 1.0, f"权重总和应为 1.0，实际 {total}"


class TestEntryConfig:
    def test_default_values(self):
        from sniper.config import EntryConfig
        cfg = EntryConfig()
        assert cfg.hard_min_price == 3.0
        assert cfg.hard_max_price == 300.0
        assert cfg.hard_min_volume == 1e6
        assert cfg.hard_not_limit_up is True
        assert cfg.soft_min_score == 79.0
        assert cfg.soft_sector_top == 3


class TestExitConfig:
    def test_default_values(self):
        from sniper.config import ExitConfig
        cfg = ExitConfig()
        assert cfg.stop_loss == -0.02
        assert cfg.trailing_stop == -0.03
        assert cfg.max_hold_days == 10


class TestRiskConfig:
    def test_default_values(self):
        from sniper.config import RiskConfig
        cfg = RiskConfig()
        assert cfg.max_positions == 5
        assert cfg.position_size == 0.07
        assert cfg.min_hold_days == 1
        assert cfg.active_reduction_l0 == 64.0
        assert cfg.active_reduction_exposure == 0.30


class TestBacktestConfig:
    def test_default_values(self):
        from sniper.config import BacktestConfig
        cfg = BacktestConfig()
        assert cfg.initial_capital == 1_000_000
        assert cfg.commission_buy == 0.00025
        assert cfg.commission_sell == 0.00025
        assert cfg.stamp_duty == 0.0005
        assert cfg.slippage == 0.001


class TestSingletonInstances:
    def test_all_instances_exist(self):
        from sniper.config import MARKET, SECTOR, STOCK, ENTRY, EXIT, RISK, BACKTEST
        assert MARKET is not None
        assert SECTOR is not None
        assert STOCK is not None
        assert ENTRY is not None
        assert EXIT is not None
        assert RISK is not None
        assert BACKTEST is not None
