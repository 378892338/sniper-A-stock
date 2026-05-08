"""K线包含处理测试"""

import numpy as np
import pandas as pd


class TestContain:
    def test_process_containment_upward(self):
        from factors.chanlun.contain import process_containment
        df = pd.DataFrame({
            "open":  [10, 11, 12],
            "close": [11, 12, 13],
            "high":  [11, 13, 14],  # 第1根被第0根包含(high 13 > 11, low 10.5 > 10)
            "low":   [10, 10.5, 11],
        })
        result = process_containment(df)
        # 第1根向上趋势时 low 10.5 > low 10, high 13 > high 11, 但应该处理包含关系
        assert "merged" in result.columns
        assert "direction" in result.columns

    def test_basic_no_containment(self):
        from factors.chanlun.contain import process_containment
        df = pd.DataFrame({
            "open":  [10, 12, 14],
            "close": [11, 13, 15],
            "high":  [11.5, 13.5, 15.5],
            "low":   [9.5, 11.5, 13.5],
        })
        result = process_containment(df)
        assert not result["merged"].iloc[1]
        assert not result["merged"].iloc[2]
        assert result["direction"].iloc[1] == 1  # 向上

    def test_downward_trend(self):
        from factors.chanlun.contain import process_containment
        df = pd.DataFrame({
            "open":  [20, 18, 16],
            "close": [19, 17, 15],
            "high":  [21, 19, 17],
            "low":   [18, 16, 14],
        })
        result = process_containment(df)
        assert result["direction"].iloc[1] == -1
        assert result["direction"].iloc[2] == -1

    def test_get_merged_bars(self):
        from factors.chanlun.contain import get_merged_bars
        df = pd.DataFrame({
            "open":  [10, 11, 12, 13],
            "close": [11, 11.5, 13, 14],
            "high":  [11.5, 11.3, 13.5, 14.5],
            "low":   [9.5, 10.8, 11.5, 12.5],
        })
        result = get_merged_bars(df)
        assert len(result) <= len(df)
