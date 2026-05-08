"""Sprint 0 基础设施层测试"""

import tempfile
import pandas as pd
import numpy as np


class TestRetry:
    def test_retry_success_first_attempt(self):
        from shared.retry import retry
        call_count = [0]

        @retry(max_retries=3, base_delay=0.01)
        def succeed():
            call_count[0] += 1
            return "ok"

        result = succeed()
        assert result == "ok"
        assert call_count[0] == 1

    def test_retry_with_failures(self):
        from shared.retry import retry
        call_count = [0]

        @retry(max_retries=3, base_delay=0.01)
        def fail_then_succeed():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ValueError("暂时失败")
            return "recovered"

        result = fail_then_succeed()
        assert result == "recovered"
        assert call_count[0] == 3

    def test_retry_exhausted(self):
        from shared.retry import retry

        @retry(max_retries=2, base_delay=0.01)
        def always_fail():
            raise RuntimeError("永久失败")

        try:
            always_fail()
            assert False, "应该抛出异常"
        except RuntimeError:
            pass

    def test_health_tracker_fail_threshold(self):
        from shared.retry import HealthTracker
        ht = HealthTracker(fail_threshold=3, recovery_threshold=2)
        assert ht.is_available("test_api")

        ht.record_failure("test_api")
        ht.record_failure("test_api")
        assert ht.is_available("test_api")  # 2次还没触发

        ht.record_failure("test_api")
        assert not ht.is_available("test_api")  # 3次触发

    def test_health_tracker_recovery(self):
        from shared.retry import HealthTracker
        ht = HealthTracker(fail_threshold=2, recovery_threshold=2)
        ht.record_failure("test_api")
        ht.record_failure("test_api")
        assert not ht.is_available("test_api")

        ht.record_success("test_api")
        ht.record_success("test_api")
        assert ht.is_available("test_api")


class TestCache:
    def test_cache_write_and_read(self):
        from shared.cache import write_cache, read_cache, CACHE_DIR
        import pandas as pd

        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        write_cache(df, "test_module", "key1", "key2")
        assert CACHE_DIR.exists()

        cached = read_cache("test_module", "key1", "key2", ttl_seconds=3600)
        assert cached is not None
        assert len(cached) == 3
        assert list(cached.columns) == ["a", "b"]

    def test_cache_miss(self):
        from shared.cache import read_cache
        result = read_cache("nonexistent", "no_key", ttl_seconds=3600)
        assert result is None

    def test_cache_key_uniqueness(self):
        from shared.cache import write_cache, read_cache, _cache_key
        import pandas as pd

        k1 = _cache_key("mod", "arg1", kw="v1")
        k2 = _cache_key("mod", "arg1", kw="v2")
        k3 = _cache_key("mod", "arg1", kw="v1")
        assert k1 != k2
        assert k1 == k3


class TestCalendar:
    def test_generate_trading_calendar(self):
        from shared.calendar import generate_trading_calendar
        cal = generate_trading_calendar("2024-01-01", "2024-01-31")
        assert len(cal) > 0
        assert cal[0].weekday() < 5

    def test_is_trading_day(self):
        from shared.calendar import is_trading_day
        import pandas as pd
        # Monday 2024-01-08 (not a holiday)
        mon = pd.Timestamp("2024-01-08")
        assert is_trading_day(mon)
        # 元旦假期 (周一但是节假日)
        holiday = pd.Timestamp("2024-01-01")
        assert not is_trading_day(holiday)
        # Saturday
        sat = pd.Timestamp("2024-01-06")
        assert not is_trading_day(sat)
        # Sunday
        sun = pd.Timestamp("2024-01-07")
        assert not is_trading_day(sun)

    def test_week_last_trading_day(self):
        from shared.calendar import week_last_trading_day
        import pandas as pd
        # Wednesday → Friday same week
        wed = pd.Timestamp("2024-01-24")
        fri = week_last_trading_day(wed)
        assert fri.weekday() == 4
        assert fri == pd.Timestamp("2024-01-26")

    def test_next_prev_trading_day(self):
        from shared.calendar import next_trading_day, prev_trading_day
        import pandas as pd
        fri = pd.Timestamp("2024-01-26")
        mon = next_trading_day(fri)
        assert mon == pd.Timestamp("2024-01-29")  # Monday
        back_to_fri = prev_trading_day(mon)
        assert back_to_fri == fri


class TestPreFilter:
    def test_filter_st_stocks(self):
        from gate.pre_filter import filter_st_stocks
        import pandas as pd
        df = pd.DataFrame({
            "symbol": ["000001", "000002", "000003", "000004"],
            "name": ["平安银行", "*ST华泽", "万科A", "ST康美"],
        })
        result = filter_st_stocks(df)
        assert len(result) == 2
        assert "000001" in result["symbol"].values
        assert "000003" in result["symbol"].values
        assert "*ST华泽" not in result["name"].values

    def test_filter_new_stocks(self):
        from gate.pre_filter import filter_new_stocks
        import pandas as pd
        from datetime import datetime, timedelta
        df = pd.DataFrame({
            "symbol": ["000001", "999999"],
            "ipo_date": [
                pd.Timestamp("2010-01-01"),
                pd.Timestamp.now() - pd.Timedelta(days=10),
            ],
        })
        result = filter_new_stocks(df, min_trading_days=60)
        assert len(result) == 1
        assert result.iloc[0]["symbol"] == "000001"

    def test_run_pre_filter_integration(self):
        from gate.pre_filter import run_pre_filter
        import pandas as pd
        from datetime import datetime, timedelta
        df = pd.DataFrame({
            "symbol": ["000001", "000002", "000003"],
            "name": ["平安银行", "*ST华泽", "万科A"],
            "ipo_date": [pd.Timestamp("2010-01-01")] * 3,
        })
        result = run_pre_filter(df)
        assert len(result) == 2


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
