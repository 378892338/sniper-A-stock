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


class TestSchedulerPreFilterIntegration:
    """Scheduler pre_filter 集成测试"""

    def test_scheduler_has_downloader(self):
        from engine.state_machine import StateMachine, SystemState
        from engine.scheduler import Scheduler
        sm = StateMachine()
        s = Scheduler(sm)
        assert s._downloader is None  # 延迟初始化
        dl = s.downloader             # 触发初始化
        assert dl is not None

    def test_run_pre_filter_returns_dataframe(self):
        from engine.state_machine import StateMachine, SystemState
        from engine.scheduler import Scheduler
        sm = StateMachine()
        s = Scheduler(sm)
        result = s._run_pre_filter()
        # 离线环境可能返回空 DataFrame，但不应该抛异常
        assert isinstance(result, pd.DataFrame)

    def test_run_weekly_cycle_includes_pre_filter(self):
        """验证 run_weekly_cycle 执行了前置过滤（通过 logger 不崩溃确认）"""
        from engine.state_machine import StateMachine, SystemState
        from engine.scheduler import Scheduler
        sm = StateMachine()
        s = Scheduler(sm)
        # 不传数据时不应崩溃
        result = s.run_weekly_cycle()
        assert "state" in result
        assert result["state"] is not None
