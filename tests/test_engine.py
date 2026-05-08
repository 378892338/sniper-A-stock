"""引擎层测试 — 状态机 + 仓位管理 + 退出追踪"""

import numpy as np
import pandas as pd


class TestStateMachine:
    def test_initial_state_idle(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        assert sm.current_state() == SystemState.IDLE

    def test_idle_to_scanning(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.ctx.recent_liquidations = 0  # 无清仓记录，不触发冷却
        sm.weekly_check(layer1_passed=True)
        assert sm.current_state() == SystemState.SCANNING

    def test_idle_stays_idle(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.weekly_check(layer1_passed=False)
        assert sm.current_state() == SystemState.IDLE

    def test_idle_to_cooling_after_liquidation(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.ctx.recent_liquidations = 1
        sm._start_cooling()
        assert sm._in_cooling()
        sm.weekly_check(layer1_passed=True)
        assert sm.current_state() == SystemState.COOLING

    def test_scanning_to_hunting(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.ctx.recent_liquidations = 0
        sm.weekly_check(layer1_passed=True)  # IDLE → SCANNING
        sm.weekly_check(layer1_passed=True, layer2_passed=True)
        assert sm.current_state() == SystemState.HUNTING

    def test_hunting_to_holding(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.ctx.recent_liquidations = 0
        sm.weekly_check(layer1_passed=True)  # IDLE → SCANNING
        sm.weekly_check(layer1_passed=True, layer2_passed=True)  # → HUNTING
        sm.on_position_opened(["000001", "000002"])
        assert sm.current_state() == SystemState.HOLDING

    def test_holding_to_idle_on_layer1_fail(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.ctx.recent_liquidations = 0
        sm.weekly_check(layer1_passed=True)
        sm.weekly_check(layer1_passed=True, layer2_passed=True)
        sm.on_position_opened(["000001"])
        # 第一层失效
        sm.weekly_check(layer1_passed=False)
        # 因为有清仓记录，会进入COOLING
        assert sm.current_state() in (SystemState.COOLING, SystemState.IDLE)

    def test_daily_alert_freezes_buy(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.ctx.recent_liquidations = 0
        sm.weekly_check(layer1_passed=True)
        sm.weekly_check(layer1_passed=True, layer2_passed=True)
        assert sm.can_buy()
        sm.on_daily_alert("橙色")
        assert not sm.can_buy()
        sm.on_daily_alert_cleared()
        assert sm.can_buy()

    def test_cross_level_jump_overrides_cooling(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.ctx.recent_liquidations = 1
        sm._start_cooling()
        assert sm._in_cooling()
        sm.weekly_check(layer1_passed=True, cross_level_jump=True)
        assert sm.current_state() == SystemState.SCANNING

    def test_stock_frozen(self):
        from engine.state_machine import StateMachine, SystemState
        sm = StateMachine()
        sm.ctx.holding_stocks = ["000001", "000002", "000003"]
        sm.on_stock_frozen("000001")
        assert "000001" in sm.ctx.frozen_stocks
        assert "000001" not in sm.ctx.holding_stocks

    def test_status_report(self):
        from engine.state_machine import StateMachine
        sm = StateMachine()
        report = sm.get_status_report()
        assert report["state"] == "IDLE"
        assert "cooling_until" in report


class TestPortfolio:
    def test_initial_portfolio(self):
        from engine.portfolio import Portfolio
        p = Portfolio(1_000_000)
        assert p.total_capital == 1_000_000
        assert p.position_pct() == 0.0

    def test_calculate_target_position(self):
        from engine.portfolio import Portfolio
        p = Portfolio(1_000_000)
        assert p.calculate_target_position("牛市") == 1.0
        assert p.calculate_target_position("震荡") == 0.50
        assert p.calculate_target_position("偏弱") == 0.30
        assert p.calculate_target_position("熊市") == 0.0

    def test_confidence_affects_target(self):
        from engine.portfolio import Portfolio
        p = Portfolio(1_000_000)
        target = p.calculate_target_position("牛市", min_confidence=0.80)
        assert target == 0.80

    def test_frozen_pct(self):
        from engine.portfolio import Portfolio, Position
        p = Portfolio(1_000_000)
        p.positions["000001"] = Position(
            symbol="000001", market_value=200_000, status="FROZEN"
        )
        p.positions["000002"] = Position(
            symbol="000002", market_value=300_000, status="NORMAL"
        )
        assert p.frozen_pct() == 0.40  # 200k / 500k

    def test_operable_position(self):
        from engine.portfolio import Portfolio, Position
        p = Portfolio(1_000_000)
        p.positions["000001"] = Position(
            symbol="000001", market_value=200_000, status="FROZEN"
        )
        assert p.operable_value() == 800_000
        assert p.operable_position_pct() == 0.80

    def test_mark_frozen_resumed(self):
        from engine.portfolio import Portfolio, Position
        p = Portfolio(1_000_000)
        p.positions["000001"] = Position(
            symbol="000001", market_value=200_000, status="NORMAL"
        )
        p.mark_frozen("000001")
        assert p.positions["000001"].status == "FROZEN"
        p.mark_resumed("000001")
        assert p.positions["000001"].status == "NORMAL"


class TestWatcher:
    def test_exit_signals_no_trigger(self):
        from gate.layer3_stock import check_exit_signals
        # 牛市数据不应触发退出信号
        n = 120
        close = pd.Series([10.0 + i * 0.04 + (i * i) * 0.0002 for i in range(n)])
        daily = pd.DataFrame({
            "open": close - 0.05, "close": close,
            "high": close + 0.1, "low": close - 0.1,
            "volume": pd.Series([1e7] * n),
            "amount": pd.Series([1e9] * n),
        })
        result = check_exit_signals("000001", daily)
        assert not result["triggered"]

    def test_exit_signals_triggered(self):
        from gate.layer3_stock import check_exit_signals
        # 持续下跌的周线数据，DIF < DEA状态
        n_w = 30
        w_close = pd.Series([50.0 - i * 0.8 for i in range(n_w)])
        weekly = pd.DataFrame({
            "open": w_close + 0.3, "close": w_close,
            "high": w_close + 0.8, "low": w_close - 0.8,
            "volume": pd.Series([1e8] * n_w),
            "amount": pd.Series([1e9] * n_w),
        })
        n = 120
        close = pd.Series([10.0 + i * 0.04 + (i*i)*0.0002 for i in range(n)])
        daily = pd.DataFrame({
            "open": close - 0.05, "close": close,
            "high": close + 0.1, "low": close - 0.1,
            "volume": pd.Series([1e7] * n),
            "amount": pd.Series([1e9] * n),
        })
        result = check_exit_signals("000001", daily, weekly_df=weekly)
        # 周线持续下跌 → DIF < DEA → 退出信号
        assert result["triggered"]
