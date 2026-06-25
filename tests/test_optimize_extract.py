"""extract_trades() 边界测试 — mock 数据，不跑回测

用构造的模拟 BacktestEngine 返回值测 extract_trades() 的所有边界情况。
"""
import tempfile
import os
import json
import pickle
from pathlib import Path

import pytest
import pandas as pd

import scripts.optimize_target as ot


# ── 辅助：构造模拟回测结果 ──

SAMPLE_PARAMS = {
    "stop_loss": -0.06,
    "trailing_stop": -0.06,
    "profit_target": 0.22,
    "max_hold_days": 43,
    "position_size": 0.07,
    "soft_min_score": 79.0,
    "bullish_threshold": 64.0,
}


def make_trade(**overrides) -> dict:
    """生成一笔模拟回测输出交易。"""
    trade = {
        "action": "SELL",
        "symbol": "600519",
        "pnl": 320.0,
        "cost": 10000.0,
        "pnl_pct": 3.2,
        "entry_date": "2026-03-15",
        "date": "2026-04-02",
        "hold_days": 18,
        "reason": "profit_target",
        "entry_price": 100.0,
        "price": 103.2,
    }
    trade.update(overrides)
    return trade


def make_result(trades=None, **overrides) -> dict:
    """生成模拟 BacktestEngine.run() 返回值。"""
    if trades is None:
        trades = [make_trade()]
    result = {
        "m": {"sharpe": 1.5, "total_return": 0.05, "total_trades": len(trades)},
        "trades": trades,
        "daily_l0_scores": {
            "2026-03-15": {"composite": 72.0, "trend": 85.0, "volume": 65.0, "breadth": 78.0},
        },
    }
    result.update(overrides)
    return result


# ── 测试类 ──


class TestExtractTradesSELLFilter:
    """① SELL 过滤器：BUY 记录、无 PnL 记录应被过滤。"""

    def test_buy_records_filtered(self):
        """BUY 记录不应出现在输出中。"""
        trades = [make_trade(action="BUY"), make_trade(action="SELL")]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert len(result) == 1
        assert result[0]["symbol"] == "600519"

    def test_no_pnl_filtered(self):
        """pnl=None 的交易不应出现在输出中。"""
        trades = [make_trade(pnl=None), make_trade(pnl=100.0)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert len(result) == 1

    def test_pnl_zero_included(self):
        """pnl=0 的交易不应被过滤（合法平仓）。"""
        trades = [make_trade(pnl=0.0, pnl_pct=0.0)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert len(result) == 1

    def test_all_buy_returns_empty(self):
        """全 BUY 无 SELL 时返回空列表。"""
        trades = [make_trade(action="BUY", pnl=None) for _ in range(5)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result == []

    def test_empty_paired_returns_empty(self):
        """空 paired 返回空列表。"""
        result = ot.extract_trades([])
        assert result == []

    def test_empty_trades_returns_empty(self):
        """无交易的结果返回空列表。"""
        paired = [(SAMPLE_PARAMS, make_result(trades=[]))]
        result = ot.extract_trades(paired)
        assert result == []


class TestExtractTradesPnLFallback:
    """② pnl_pct fallback：当 pnl_pct 为 None 时从 pnl/cost 自算。"""

    def test_pnl_pct_direct_use(self):
        """pnl_pct 有值时直接使用。"""
        trades = [make_trade(pnl_pct=5.5)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["pnl_pct"] == 5.5

    def test_pnl_pct_fallback(self):
        """pnl_pct 为 None 时从 pnl/cost 自算。"""
        trades = [make_trade(pnl_pct=None, pnl=500.0, cost=10000.0)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["pnl_pct"] == 0.05  # 500/10000

    def test_pnl_pct_zero_cost(self):
        """cost=0 时 pnl_pct=0（防除零）。"""
        trades = [make_trade(pnl_pct=None, pnl=100.0, cost=0)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["pnl_pct"] == 0.0

    def test_pnl_pct_zero_pnl(self):
        """pnl=0, cost>0 时 pnl_pct=0。"""
        trades = [make_trade(pnl_pct=None, pnl=0.0, cost=10000.0)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["pnl_pct"] == 0.0

    def test_pnl_pct_both_none(self):
        """pnl_pct=None, pnl=0 时从 pnl/cost 自算得 0。"""
        trades = [make_trade(pnl_pct=None, pnl=0, cost=10000.0)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["pnl_pct"] == 0.0


class TestExtractTradesEntryDate:
    """③ entry_date 边界。"""

    def test_missing_entry_date_skipped(self):
        """entry_date 为空时跳过。"""
        trades = [make_trade(entry_date="", action="SELL", pnl=100.0)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert len(result) == 0

    def test_invalid_date_hold_days_zero(self):
        """日期解析失败时 hold_days=0，不跳过。"""
        trades = [make_trade(entry_date="not-a-date", date="also-bad")]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert len(result) == 1
        assert result[0]["hold_days"] == 0


class TestExtractTradesHoldDays:
    """④ hold_days 计算。"""

    def test_hold_days_calculated(self):
        """从 entry_date 和 exit_date 正确计算持仓天数。"""
        trades = [make_trade(entry_date="2026-03-15", date="2026-04-02", pnl_pct=None)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        # 2026-03-15 → 2026-04-02 = 18 天
        assert result[0]["hold_days"] == 18

    def test_hold_days_min_one(self):
        """同日买入卖出时 hold_days=1（最少 1 天）。"""
        trades = [make_trade(entry_date="2026-03-15", date="2026-03-15", pnl_pct=None)]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["hold_days"] == 1


class TestExtractTradesMarketFingerprint:
    """⑤ 市场指纹关联。"""

    def test_l0_scores_from_daily(self):
        """从 daily_l0_scores 正确关联入场日的市场指纹。"""
        trades = [make_trade(entry_date="2026-03-15")]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["l0_score"] == 72.0
        assert result[0]["l0_trend"] == 85.0
        assert result[0]["l0_volume"] == 65.0
        assert result[0]["l0_breadth"] == 78.0
        assert result[0]["market_state_vector"] == [72.0, 85.0, 65.0, 78.0]

    def test_l0_missing_date_defaults(self):
        """daily_l0_scores 中缺入场日时默认 50.0。"""
        trades = [make_trade(entry_date="2099-01-01")]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["l0_score"] == 50.0
        assert result[0]["l0_trend"] == 50.0

    def test_l0_empty_daily_scores(self):
        """daily_l0_scores 为空时所有维度默认 50.0。"""
        trades = [make_trade(entry_date="2026-03-15")]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades, daily_l0_scores={}))]
        result = ot.extract_trades(paired)
        assert result[0]["l0_score"] == 50.0


class TestExtractTradesOutputFields:
    """⑥ 输出字段完整性。"""

    def test_params_preserved(self):
        """params 应原样保留到输出。"""
        trades = [make_trade()]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert result[0]["params"] == SAMPLE_PARAMS

    def test_all_key_fields_present(self):
        """输出应包含所有必要字段。"""
        trades = [make_trade()]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        record = result[0]
        required_keys = {
            "params", "pnl_pct", "l0_score", "l0_trend", "l0_volume", "l0_breadth",
            "market_state_vector", "entry_date", "exit_date", "hold_days",
            "exit_reason", "symbol",
        }
        missing = required_keys - set(record.keys())
        assert not missing, f"缺少字段: {missing}"

    def test_multi_param_sets_all_present(self):
        """多组参数的多笔交易全部输出。"""
        paired = [
            ({"stop_loss": -0.10}, make_result(trades=[make_trade(symbol="A")])),
            ({"stop_loss": -0.05}, make_result(trades=[make_trade(symbol="B")])),
        ]
        result = ot.extract_trades(paired)
        assert len(result) == 2
        symbols = {r["symbol"] for r in result}
        assert symbols == {"A", "B"}

    def test_multi_trades_per_param_set(self):
        """一组参数的多笔交易全部输出。"""
        trades = [make_trade(symbol="A"), make_trade(symbol="B"), make_trade(symbol="C")]
        paired = [(SAMPLE_PARAMS, make_result(trades=trades))]
        result = ot.extract_trades(paired)
        assert len(result) == 3


class TestSnapToGrid:
    """⑦ _snap_to_grid 边界。"""

    def test_int_param(self):
        """整数参数正确步进。"""
        meta = {"lo": 5, "hi": 55, "step": 10, "int": True}
        assert ot._snap_to_grid(53, meta) == 50
        assert ot._snap_to_grid(57, meta) == 55  # snap→60, clamp→55

    def test_clip_below_lo(self):
        """低于下限时 clamp 到 lo。"""
        meta = {"lo": 5, "hi": 55, "step": 10, "int": True}
        assert ot._snap_to_grid(1, meta) == 5

    def test_clip_above_hi(self):
        """高于上限时 clamp 到 hi。"""
        meta = {"lo": 5, "hi": 55, "step": 10, "int": True}
        assert ot._snap_to_grid(60, meta) == 55  # 不越界

    def test_float_param(self):
        """浮点参数正确步进。"""
        meta = {"lo": -0.20, "hi": -0.02, "step": 0.01, "int": False}
        result = ot._snap_to_grid(-0.075, meta)
        assert abs(result - (-0.08)) < 1e-10

    def test_non_divisible_hi(self):
        """hi-lo 不被 step 整除时，snap 后 clamp 不越界。"""
        meta = {"lo": 0, "hi": 18, "step": 5, "int": True}
        assert ot._snap_to_grid(18, meta) <= 18  # 不会 snap 到 20


class TestSaveLoadRawResults:
    """⑧ _save_raw_results / _load_raw_results 周期。"""

    def test_save_load_roundtrip(self, monkeypatch):
        """保存后加载应返回相同数据。"""
        combos = [{"stop_loss": -0.06}, {"stop_loss": -0.05}]
        results = [{"m": {}, "trades": []}, {"m": {}, "trades": []}]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "raw_results.pkl"
            monkeypatch.setattr(ot, "_RAW_RESULTS_FILE", tmp_path)
            ot._save_raw_results(combos, results)
            loaded_c, loaded_r = ot._load_raw_results()
            assert loaded_c == combos
            assert loaded_r == results

    def test_load_missing_returns_none(self, monkeypatch):
        """文件不存在时返回 (None, None)。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "nonexistent.pkl"
            monkeypatch.setattr(ot, "_RAW_RESULTS_FILE", tmp_path)
            c, r = ot._load_raw_results()
            assert c is None
            assert r is None

    def test_corrupted_file_returns_none(self, monkeypatch):
        """损坏的 pickle 返回 (None, None) 而非崩溃。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "bad.pkl"
            tmp_path.write_bytes(b"not a pickle")
            monkeypatch.setattr(ot, "_RAW_RESULTS_FILE", tmp_path)
            c, r = ot._load_raw_results()
            assert c is None
            assert r is None
