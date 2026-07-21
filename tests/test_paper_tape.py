"""打字机纸带 roundtrip 测试 — dict → parquet(展平) → 重建 dict"""

import tempfile
import os

import pandas as pd

import sniper.config as cfg


# ── 辅助：生成模拟纸带记录 ──

SAMPLE_PARAMS = {
    "stop_loss": -0.06,
    "trailing_stop": -0.06,
    "profit_target": 0.22,
    "max_hold_days": 43,
    "position_size": 0.07,
    "soft_min_score": 79.0,
    "bullish_threshold": 64.0,
}

SAMPLE_VECTOR = [72.0, 85.0, 65.0, 78.0]


def make_trade(**overrides) -> dict:
    """生成一笔模拟纸带记录。

    如果 overrides 的 key 是 SAMPLE_PARAMS 中的参数名，自动归入 params。
    """
    params = dict(SAMPLE_PARAMS)
    top_overrides = {}
    for k, v in overrides.items():
        if k in SAMPLE_PARAMS:
            params[k] = v
        else:
            top_overrides[k] = v
    trade = {
        "params": params,
        "pnl_pct": 3.2,
        "hold_days": 15,
        "exit_reason": "profit_target",
        "symbol": "600519",
        "entry_date": "2026-03-15",
        "exit_date": "2026-04-02",
        "l0_score": 72.0,
        "l0_trend": 85.0,
        "l0_volume": 65.0,
        "l0_breadth": 78.0,
        "market_state_vector": list(SAMPLE_VECTOR),
    }
    trade.update(top_overrides)
    return trade


def save_flattened_parquet(records: list[dict], path: str) -> None:
    """模拟 optimize_target.py 的展平写入逻辑（含 Config 全参快照）。"""
    df = pd.DataFrame(records)
    params_expanded = df["params"].apply(pd.Series)
    params_expanded.columns = [f"param_{c}" for c in params_expanded.columns]
    vector_df = df["market_state_vector"].apply(pd.Series)
    vector_df.columns = ["msv_l0", "msv_trend", "msv_volume", "msv_breadth"]
    df_flat = pd.concat([
        df.drop(columns=["params", "market_state_vector"]),
        params_expanded,
        vector_df,
    ], axis=1)
    df_flat.to_parquet(path, index=False)


# ── 测试 ①：parquet 读写 roundtrip ──


class TestPaperTapeRoundtrip:

    def _load_and_get(self, trades, tmp_path):
        """辅助：写入展平 parquet → load → 返回 cfg._TRADE_PAPER。"""
        path = os.path.join(tmp_path, "tape.parquet")
        save_flattened_parquet(trades, path)
        cfg.load_paper_tape(path)
        return cfg._TRADE_PAPER

    def test_load_reconstructs_params(self):
        """load_paper_tape() 从展平 parquet 重建 params 嵌套字段。"""
        trades = [make_trade() for _ in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            tape = self._load_and_get(trades, tmp)
        assert tape is not None
        assert len(tape) == 5
        first = tape[0]
        assert "params" in first, "应重建 params 字段"
        assert first["params"] == SAMPLE_PARAMS, "params 内容应与原始一致"
        assert "param_stop_loss" not in first, "应移除展平字段 param_*"

    def test_load_reconstructs_vector(self):
        """load_paper_tape() 重建 market_state_vector。"""
        with tempfile.TemporaryDirectory() as tmp:
            tape = self._load_and_get([make_trade()], tmp)
        first = tape[0]
        assert "market_state_vector" in first
        assert first["market_state_vector"] == SAMPLE_VECTOR
        assert "msv_l0" not in first
        assert "msv_trend" not in first

    def test_load_preserves_scalars(self):
        """标量字段（pnl_pct, symbol, entry_date 等）应保持不变。"""
        trades = [make_trade(pnl_pct=-1.5, symbol="000001", entry_date="2026-01-01")]
        with tempfile.TemporaryDirectory() as tmp:
            tape = self._load_and_get(trades, tmp)
        first = tape[0]
        assert first["pnl_pct"] == -1.5
        assert first["symbol"] == "000001"
        assert first["entry_date"] == "2026-01-01"

    def test_missing_file_returns_none(self):
        """文件不存在时，_TRADE_PAPER 应为 None。"""
        cfg._TRADE_PAPER = None
        cfg.load_paper_tape("/nonexistent/path.parquet")
        assert cfg._TRADE_PAPER is None

    def test_multi_trade_params_independent(self):
        """多笔交易的每笔 params 应独立。"""
        trades = [
            make_trade(position_size=0.05),
            make_trade(position_size=0.15),
            make_trade(position_size=0.25),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tape = self._load_and_get(trades, tmp)
        assert tape[0]["params"]["position_size"] == 0.05
        assert tape[1]["params"]["position_size"] == 0.15
        assert tape[2]["params"]["position_size"] == 0.25


# ── 测试 ②：核心函数行为 ──


class TestMarketFunctions:

    def _load_and_get(self, trades, tmp_path):
        path = os.path.join(tmp_path, "tape.parquet")
        save_flattened_parquet(trades, path)
        cfg.load_paper_tape(path)
        return cfg._TRADE_PAPER

    def test_market_distance_identical(self):
        """相同指纹的距离应为 0。"""
        fp = [50.0, 50.0, 50.0, 50.0]
        assert cfg._market_distance(fp, fp) == 0.0

    def test_market_distance_symmetric(self):
        """距离应对称。"""
        a = [50.0, 60.0, 70.0, 80.0]
        b = [80.0, 70.0, 60.0, 50.0]
        assert cfg._market_distance(a, b) == cfg._market_distance(b, a)

    def test_market_distance_weighted(self):
        """L0 维度不同的影响应大于其他维度（权重 0.50）。"""
        d1 = cfg._market_distance([60, 50, 50, 50], [50, 50, 50, 50])
        d2 = cfg._market_distance([50, 60, 50, 50], [50, 50, 50, 50])
        assert d1 > d2, "L0(权重0.5)的变化应大于趋势(权重0.2)"

    def test_find_neighbors_empty(self):
        """无纸带时返回空列表。"""
        old = cfg._TRADE_PAPER
        cfg._TRADE_PAPER = None
        try:
            result = cfg._find_neighbors([50, 50, 50, 50])
            assert result == []
        finally:
            cfg._TRADE_PAPER = old

    def test_find_neighbors_missing_fingerprint(self):
        """缺 market_state_vector 的交易不应入选近邻。"""
        trades = [make_trade() for _ in range(15)]
        trades[0].pop("market_state_vector", None)
        with tempfile.TemporaryDirectory() as tmp:
            self._load_and_get(trades, tmp)
            neighbors = cfg._find_neighbors([72.0, 85.0, 65.0, 78.0])
        assert neighbors, "应有近邻"
        assert all("market_state_vector" in t for t in neighbors), "缺指纹交易不应入选"


# ── 测试 ③：B方案（空信号/部分信号） ──


class TestBAttribution:

    def _load_and_get(self, trades, tmp_path):
        path = os.path.join(tmp_path, "tape.parquet")
        save_flattened_parquet(trades, path)
        cfg.load_paper_tape(path)
        return cfg._TRADE_PAPER

    def test_attribution_all_no_signal(self):
        """所有参数 win_median ≈ lose_median → 返回空 dict。"""
        # 构造交易：赚钱和亏钱的参数值几乎一样 → impact≈0
        trades = []
        for _ in range(10):
            trades.append(make_trade(position_size=0.10, stop_loss=-0.06,
                                     trailing_stop=-0.06, profit_target=0.22,
                                     max_hold_days=43, soft_min_score=79.0,
                                     bullish_threshold=64.0, pnl_pct=3.0))
        for _ in range(10):
            trades.append(make_trade(position_size=0.10, stop_loss=-0.06,
                                     trailing_stop=-0.06, profit_target=0.22,
                                     max_hold_days=43, soft_min_score=79.0,
                                     bullish_threshold=64.0, pnl_pct=-2.0))
        result = cfg._attribution(trades)
        assert result == {}, "全无信号时应返回空 dict"

    def test_attribution_partial_signal(self):
        """只有 position_size 有区分度 → 只输出 position_size。"""
        trades = []
        # 亏钱组：position_size=0.05
        for _ in range(5):
            trades.append(make_trade(position_size=0.05, stop_loss=-0.06,
                                     trailing_stop=-0.06, profit_target=0.22,
                                     max_hold_days=43, soft_min_score=79.0,
                                     bullish_threshold=64.0, pnl_pct=-2.0))
        # 赚钱组：position_size=0.20
        for _ in range(5):
            trades.append(make_trade(position_size=0.20, stop_loss=-0.06,
                                     trailing_stop=-0.06, profit_target=0.22,
                                     max_hold_days=43, soft_min_score=79.0,
                                     bullish_threshold=64.0, pnl_pct=3.0))
        result = cfg._attribution(trades)
        assert "position_size" in result, "position_size 应有信号"
        assert result["position_size"] > 0.10, "赚钱组 position_size 更大"
        # 其他参数无区分度 → 不输出
        for k in ("stop_loss", "trailing_stop", "profit_target",
                  "max_hold_days", "soft_min_score", "bullish_threshold"):
            assert k not in result, f"{k} 无区分度不应输出"

    def test_configure_for_today_empty_result(self):
        """_attribution 返回 {} → configure_for_today 不更新全局参数。"""
        old_exit = cfg.EXIT
        old_risk = cfg.RISK
        old_entry = cfg.ENTRY
        old_market = cfg.MARKET
        # 造一组全部相同参数值的交易 → 无区分度
        trades = []
        for _ in range(10):
            trades.append(make_trade(pnl_pct=3.0))
        for _ in range(10):
            trades.append(make_trade(pnl_pct=-2.0))
        with tempfile.TemporaryDirectory() as tmp:
            self._load_and_get(trades, tmp)
            cfg.configure_for_today(72.0, 85.0, 65.0, 78.0)
        assert cfg.EXIT is old_exit, "EXIT 不应被更新"
        assert cfg.RISK is old_risk, "RISK 不应被更新"
        assert cfg.ENTRY is old_entry, "ENTRY 不应被更新"
        assert cfg.MARKET is old_market, "MARKET 不应被更新"

    def test_profit_impact_small_sample(self):
        """不足 10 笔时返回零 impact。"""
        trades = [make_trade(pnl_pct=i) for i in range(5)]
        result = cfg.profit_impact(trades, "stop_loss")
        assert result["impact"] == 0.0
        # 新接口字段
        assert "win_median" in result
        assert "lose_median" in result

    def test_profit_impact_deterministic(self):
        """参数值越大 PnL 越高时，impact 应为正。
        必须同时有赚钱和亏钱交易，否则任一组为空 → impact=0。
        """
        trades = []
        # 5 笔亏钱（小参数值）
        for size in [0.05, 0.06, 0.07, 0.08, 0.09]:
            t = make_trade(position_size=size, pnl_pct=-2.0)
            trades.append(t)
        # 5 笔赚钱（大参数值）
        for size in [0.15, 0.18, 0.20, 0.22, 0.25]:
            pnl = 3.0 + size * 10
            t = make_trade(position_size=size, pnl_pct=pnl)
            trades.append(t)
        result = cfg.profit_impact(trades, "position_size")
        assert result["impact"] > 0, "大参数赚更多时 impact 应为正"
        assert result["win_median"] > result["lose_median"]

    def test_profit_impact_groups_too_small(self):
        """任一组 < 3 笔时 impact=0。"""
        # 赚钱组 2 笔，亏钱组 8 笔
        trades = []
        for _ in range(2):
            trades.append(make_trade(position_size=0.10, pnl_pct=3.0))
        for _ in range(8):
            trades.append(make_trade(position_size=0.10, pnl_pct=-1.0))
        result = cfg.profit_impact(trades, "position_size")
        assert result["impact"] == 0.0


# ── 测试 ③：append_to_paper_tape ──


class TestAppendToPaperTape:

    def _init_tape(self, trades, tmp_path):
        """辅助：写入初始纸带 + 加载到内存。"""
        path = os.path.join(tmp_path, "paper_tape.parquet")
        cfg._TRADE_PAPER = None
        save_flattened_parquet(trades, path)
        cfg.load_paper_tape(path)
        return path

    def test_append_increases_count(self):
        """追加后纸带记录数 +1。"""
        trades = [make_trade() for _ in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            path = self._init_tape(trades, tmp)
            new_trade = make_trade(symbol="999999", pnl_pct=5.5)
            cfg.append_to_paper_tape(new_trade, path_override=path)
            assert len(cfg._TRADE_PAPER) == 6

    def test_append_preserves_original(self):
        """追加后原始交易数据不变。"""
        trades = [make_trade(symbol="000001", pnl_pct=1.0)]
        with tempfile.TemporaryDirectory() as tmp:
            path = self._init_tape(trades, tmp)
            new_trade = make_trade(symbol="000002", pnl_pct=2.0)
            cfg.append_to_paper_tape(new_trade, path_override=path)
            assert cfg._TRADE_PAPER[0]["symbol"] == "000001"
            assert cfg._TRADE_PAPER[0]["pnl_pct"] == 1.0

    def test_append_reconstructs_params(self):
        """追加后的交易 params 正确重建。"""
        trades = [make_trade() for _ in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            path = self._init_tape(trades, tmp)
            new_trade = make_trade(position_size=0.20, stop_loss=-0.15)
            cfg.append_to_paper_tape(new_trade, path_override=path)
            last = cfg._TRADE_PAPER[-1]
            assert "params" in last
            assert last["params"]["position_size"] == 0.20
            assert last["params"]["stop_loss"] == -0.15

    def test_append_reconstructs_vector(self):
        """追加后的交易 market_state_vector 正确重建。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._init_tape([make_trade()], tmp)
            cfg.append_to_paper_tape(make_trade(), path_override=path)
            last = cfg._TRADE_PAPER[-1]
            assert "market_state_vector" in last
            assert last["market_state_vector"] == [72.0, 85.0, 65.0, 78.0]

    def test_append_no_memory_cache(self):
        """无内存缓存时只写 parquet，不崩溃。"""
        cfg._TRADE_PAPER = None
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "paper_tape.parquet")
            # 先写一个初始纸带（但不加载到内存）
            save_flattened_parquet([make_trade()], path)
            # 然后追加
            cfg.append_to_paper_tape(make_trade(), path_override=path)
            # 验证文件存在且可读
            df = pd.read_parquet(path)
            assert len(df) == 2
