"""sniper/engine/ 各模块单元测试"""

import pytest


class TestRiskManager:
    """仓位管理与风控测试"""

    def test_import(self):
        from sniper.engine.risk import RiskManager
        assert RiskManager is not None

    def test_initial_state(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        assert rm.initial_capital == 1_000_000
        assert rm.cash == 1_000_000
        assert rm.peak_capital == 1_000_000
        assert rm.position_count == 0
        assert rm.total_exposure == 0.0
        assert rm.total_capital == 1_000_000

    def test_can_open_new_initially(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        assert rm.can_open_new() is True

    def test_cannot_open_when_at_max(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        # 风控配置 max_positions=5，开满后不能继续开
        for i in range(5):
            price = 10.0 + i
            rm.open_position(f"00000{i}", price, "2024-06-01")
        # 第 6 个被 max_positions 挡住
        pos = rm.open_position("000005", 10.0, "2024-06-01")
        assert pos is None
        assert rm.can_open_new() is False

    def test_cannot_open_when_no_cash(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1000)
        # cash 极小，size 计算后 cost > cash
        pos = rm.open_position("000001", 100.0, "2024-06-01")
        assert pos is None

    def test_open_position_success(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        pos = rm.open_position("000001", 10.0, "2024-06-01", sector="计算机", score=85.0)
        assert pos is not None
        assert pos["symbol"] == "000001"
        assert pos["entry_price"] == 10.0
        assert pos["sector"] == "计算机"
        assert pos["score"] == 85.0
        assert rm.position_count == 1
        assert rm.cash < 1_000_000  # 扣除了成本
        assert len(rm.trades) == 1
        assert rm.trades[0]["action"] == "BUY"

    def test_open_position_shares_rounded_to_lot(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        pos = rm.open_position("000001", 10.0, "2024-06-01")
        assert pos is not None
        # 仓位 = 50%目标暴露 / 5个仓位 = 100_000
        # shares = int(100000 / 10 / 100) * 100 = 10000
        # cost = 10000 * 10 * (1 + 0.00025 + 0.001) = 100125
        assert pos["shares"] == 10000
        assert pos["cost"] == 100125.0

    def test_close_position(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        rm.open_position("000001", 10.0, "2024-06-01")
        result = rm.close_position("000001", 11.0, "2024-06-10", reason="目标止盈")
        assert result is not None
        assert result["pnl"] > 0
        # PnL = 10000*11*(1-0.00025-0.0005-0.001) - 10000*10*(1+0.00025+0.001)
        #     = 109807.5 - 100125 = 9682.5
        # pnl_pct = 9682.5 / 100125 = 0.0967
        assert result["pnl_pct"] == pytest.approx(0.0967, abs=1e-3)
        assert rm.position_count == 0
        assert len(rm.trades) == 2  # BUY + SELL

    def test_close_nonexistent_position(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        result = rm.close_position("nonexistent", 10.0, "2024-06-01")
        assert result is None

    def test_update_positions(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        rm.open_position("000001", 10.0, "2024-06-01")
        rm.update_positions({"000001": 11.0})
        pos = rm.positions["000001"]
        assert pos["market_value"] == pos["shares"] * 11.0
        assert pos["pnl"] > 0
        assert pos["highest_price"] == 11.0

    def test_daily_report(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        report = rm.daily_report("2024-06-01")
        assert report["date"] == "2024-06-01"
        assert report["total_value"] == 1_000_000
        assert report["drawdown"] == 0.0
        assert len(rm.daily_values) == 1

    def test_check_max_loss_no_loss(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        assert rm.check_max_loss() is False  # 未跌破 max_total_loss

    def test_get_position_size(self):
        from sniper.engine.risk import RiskManager
        rm = RiskManager(1_000_000)
        size = rm.get_position_size()
        assert size == 1_000_000 * 0.50 / 5  # target_exposure_ratio=0.50, max_positions=5


class TestMetrics:
    """绩效指标计算测试"""

    def test_import(self):
        from sniper.engine.metrics import calculate_metrics, print_metrics_table
        assert calculate_metrics is not None
        assert print_metrics_table is not None

    def test_calculate_metrics_empty(self):
        from sniper.engine.metrics import calculate_metrics
        result = calculate_metrics([], [], 1_000_000)
        assert result == {}

    def test_calculate_metrics_basic(self):
        from sniper.engine.metrics import calculate_metrics
        daily_values = [
            {"date": "2024-01-01", "total_value": 1_000_000},
            {"date": "2024-01-02", "total_value": 1_010_000},
            {"date": "2024-01-03", "total_value": 1_020_000},
        ]
        trades = [
            {"action": "SELL", "pnl": 10000, "symbol": "000001"},
            {"action": "SELL", "pnl": 10000, "symbol": "000002"},
        ]
        result = calculate_metrics(daily_values, trades, 1_000_000)
        assert result["total_return"] == 0.02  # 2%
        assert result["total_trades"] == 2
        assert result["win_rate"] == 1.0
        assert result["final_capital"] == 1_020_000
        assert result["sharpe"] >= 0  # 正的收益 → 正的 Sharpe

    def test_calculate_metrics_with_loss_trades(self):
        from sniper.engine.metrics import calculate_metrics
        daily_values = [
            {"date": "2024-01-01", "total_value": 1_000_000},
            {"date": "2024-01-02", "total_value": 990_000},
            {"date": "2024-01-03", "total_value": 1_010_000},
        ]
        trades = [
            {"action": "SELL", "pnl": 20000, "symbol": "000001"},
            {"action": "SELL", "pnl": -10000, "symbol": "000002"},
            {"action": "SELL", "pnl": -5000, "symbol": "000003"},
        ]
        result = calculate_metrics(daily_values, trades, 1_000_000)
        assert result["total_trades"] == 3
        # win_rate 被 round(win_rate, 4) 截断为 0.3333
        assert result["win_rate"] == 0.3333
        assert result["avg_win"] == 20000
        assert result["avg_loss"] == -7500

    def test_print_metrics_table_does_not_crash(self):
        from sniper.engine.metrics import print_metrics_table
        metrics = {"total_return": 0.05, "sharpe": 1.5}
        # 只是不抛异常
        print_metrics_table(metrics)
        print_metrics_table({})

    def test_calculate_metrics_profit_factor(self):
        from sniper.engine.metrics import calculate_metrics
        daily_values = [
            {"date": "2024-01-01", "total_value": 1_000_000},
            {"date": "2024-01-02", "total_value": 1_000_000},
        ]
        trades = [
            {"action": "SELL", "pnl": 30000, "symbol": "A"},
            {"action": "SELL", "pnl": -10000, "symbol": "B"},
        ]
        result = calculate_metrics(daily_values, trades, 1_000_000)
        # profit_factor = (30000) / (10000 + 1) ≈ 3.0
        assert result["profit_factor"] > 2.0


class TestBacktestEngine:
    """回测引擎测试"""

    def test_import(self):
        from sniper.engine.backtest import BacktestEngine
        assert BacktestEngine is not None

    def test_initialization(self, router):
        from sniper.engine.backtest import BacktestEngine
        engine = BacktestEngine(router)
        assert engine.market is not None
        assert engine.sector is not None
        assert engine.stock_scorer is not None
        assert engine.entry is not None
        assert engine.exit_chain is not None
        assert engine.risk is not None
        assert engine.risk.initial_capital == 1_000_000
        assert engine.daily_logs == []

    def test_get_entry_price_no_data(self, router):
        from sniper.engine.backtest import BacktestEngine
        engine = BacktestEngine(router)
        price = engine._get_entry_price("000001", "2024-06-01")
        assert price is None

    def test_get_entry_price_with_data(self, router):
        from sniper.engine.backtest import BacktestEngine
        import pandas as pd
        dates = pd.DatetimeIndex(["2024-06-03"])
        df = pd.DataFrame({"open": [15.5], "close": [15.8]}, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        engine = BacktestEngine(router)
        price = engine._get_entry_price("000001", "2024-06-03")
        assert price == 15.5

    def test_get_exit_price_no_data(self, router):
        from sniper.engine.backtest import BacktestEngine
        engine = BacktestEngine(router)
        price = engine._get_exit_price("000001", "2024-06-01")
        assert price is None

    def test_get_exit_price_with_data(self, router):
        from sniper.engine.backtest import BacktestEngine
        import pandas as pd
        dates = pd.DatetimeIndex(["2024-06-03"])
        df = pd.DataFrame({"open": [15.5], "close": [15.8]}, index=dates)
        df["date"] = df.index
        router.set_daily_bars("000001", df)
        engine = BacktestEngine(router)
        price = engine._get_exit_price("000001", "2024-06-03")
        assert price == 15.8

    def test_run_empty_calendar(self, router):
        """无交易日历 → 空结果"""
        from sniper.engine.backtest import BacktestEngine
        engine = BacktestEngine(router)
        result = engine.run("2024-01-01", "2024-01-10")
        assert result == {}

    def test_run_with_basic_data(self, router, sample_daily_bars, sample_index_bars):
        """迷你回测场景 — 仅验证不抛异常，有结果返回"""
        import pandas as pd
        from sniper.engine.backtest import BacktestEngine

        # 设置指数（让 L0 输出非 bearish）
        router.set_index_bars("shanghai", sample_index_bars)

        # 设置板块数据（让 L1 能选出板块）
        router._industry_data = pd.DataFrame({
            "date": ["2024-01-02"],
            "industry_name": ["计算机"],
            "daily_change": [0.02],
            "rank": [1],
        })

        # 设置少量个股
        router.set_daily_bars("000001", sample_daily_bars)

        # 设置交易日历（只跑 10 天快速验证）
        dates = list(pd.bdate_range("2024-01-01", periods=10))
        router.set_trading_dates([str(d)[:10] for d in dates])

        engine = BacktestEngine(router)
        result = engine.run("2024-01-01", "2024-01-10")
        assert isinstance(result, dict)
        assert "daily_values" in result

    def test_build_result_structure(self, router):
        from sniper.engine.backtest import BacktestEngine
        engine = BacktestEngine(router)
        # 直接调 _build_result 验证结构
        result = engine._build_result()
        assert "final_capital" in result
        assert "total_return" in result
        assert "trades" in result
        assert "daily_values" in result
        assert "logs" in result
