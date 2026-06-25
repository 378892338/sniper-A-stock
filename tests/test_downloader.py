"""测试 data/downloader.py + scheduler pre_filter 集成"""

import pandas as pd
import pytest


class TestDataDownloader:
    """DataDownloader 单元测试"""

    def test_import(self):
        from data.downloader import DataDownloader
        dl = DataDownloader()
        assert dl is not None

    def test_needs_refresh_initial(self):
        from data.downloader import DataDownloader
        dl = DataDownloader()
        assert dl.needs_refresh("market_shanghai") is True
        assert dl.needs_refresh("stock_list") is True

    def test_last_refresh_tracking(self):
        from data.downloader import DataDownloader
        dl = DataDownloader()
        assert dl.get_last_refresh("nonexistent") == 0
        dl._last_refresh["test"] = 999
        assert dl.get_last_refresh("test") == 999

    def test_resample_to_weekly(self):
        from data.downloader import DataDownloader
        dl = DataDownloader()
        idx = pd.date_range("2024-01-01", "2024-01-31", freq="B")
        df = pd.DataFrame({
            "open": range(len(idx)), "close": range(len(idx)),
            "high": range(len(idx)), "low": range(len(idx)),
            "volume": [1000] * len(idx),
        }, index=idx)
        weekly = dl.resample_to_weekly(df)
        assert not weekly.empty
        assert "open" in weekly.columns
        assert "close" in weekly.columns

    def test_resample_to_monthly(self):
        from data.downloader import DataDownloader
        dl = DataDownloader()
        idx = pd.date_range("2024-01-01", "2024-03-31", freq="B")
        df = pd.DataFrame({
            "open": range(len(idx)), "close": range(len(idx)),
            "high": range(len(idx)), "low": range(len(idx)),
            "volume": [1000] * len(idx),
        }, index=idx)
        monthly = dl.resample_to_monthly(df)
        assert not monthly.empty


class TestPipelineIntegration:
    """数据管道集成测试 — 验证新 pipeline + pre_filter 组件"""

    def test_pipeline_imports(self):
        """验证新 pipeline 组件能正确导入"""
        from data.pre_filter import filter_st_stocks, filter_new_stocks, run_pre_filter
        from data.pipeline import download_and_update, prepare_backtest, export_backtest_snapshot
        assert filter_st_stocks is not None
        assert filter_new_stocks is not None
        assert run_pre_filter is not None
        assert download_and_update is not None
        assert prepare_backtest is not None
        assert export_backtest_snapshot is not None

    def test_pre_filter_returns_dataframe_with_empty_input(self):
        """pre_filter 接收空 DataFrame 应返回空 DataFrame 而非抛异常"""
        from data.pre_filter import run_pre_filter
        import pandas as pd

        result = run_pre_filter(pd.DataFrame())
        assert isinstance(result, pd.DataFrame)

    def test_pre_filter_handles_missing_columns(self):
        """pre_filter 缺少必要列时应返回原始 DataFrame"""
        from data.pre_filter import run_pre_filter
        import pandas as pd

        df = pd.DataFrame({"symbol": ["000001"]})  # 缺少 name / ipo_date
        result = run_pre_filter(df)
        assert len(result) == 1  # 缺少列不报错

    def test_sniper_data_router_imports(self):
        """验证 sniper/ 核心组件能正确导入"""
        from sniper.data_router import DataRouter
        from sniper.layers.l0_market import MarketScorer
        from sniper.layers.l1_sector import SectorScorer
        from sniper.layers.l2_stock import StockScorer
        from sniper.layers.l3_entry import EntryFilter
        from sniper.layers.l4_exit import ExitChain
        from sniper.engine.backtest import BacktestEngine
        from sniper.engine.risk import RiskManager
        assert DataRouter is not None
        assert MarketScorer is not None
        assert SectorScorer is not None
        assert StockScorer is not None
        assert EntryFilter is not None
        assert ExitChain is not None
        assert BacktestEngine is not None
        assert RiskManager is not None
