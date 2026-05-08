"""顶底分型识别测试"""

import numpy as np
import pandas as pd


class TestFractal:
    def test_top_fractal(self):
        from factors.chanlun.fractal import identify_fractals
        df = pd.DataFrame({
            "open":  [10, 12, 15, 13],
            "close": [12, 15, 13, 11],
            "high":  [12.5, 15.5, 16, 13.5],
            "low":   [9, 11, 12, 10],
        })
        result = identify_fractals(df)
        # 第2根 (i=2): high[2]=16 > high[1]=15.5 且 > high[3]=13.5
        assert result["top_fractal"].iloc[2]

    def test_bottom_fractal(self):
        from factors.chanlun.fractal import identify_fractals
        df = pd.DataFrame({
            "open":  [15, 12, 9, 11],
            "close": [12, 9, 11, 13],
            "high":  [16, 13, 11, 14],
            "low":   [11, 8, 7, 9],
        })
        result = identify_fractals(df)
        # 第2根 (i=2): low[2]=7 < low[1]=8 且 < low[3]=9
        assert result["bottom_fractal"].iloc[2]

    def test_no_fractal_on_edge(self):
        from factors.chanlun.fractal import identify_fractals
        df = pd.DataFrame({
            "open":  [20, 15, 10],
            "close": [15, 10, 12],
            "high":  [21, 16, 12],
            "low":   [14, 9, 8],
        })
        result = identify_fractals(df)
        assert not result["top_fractal"].iloc[0]
        assert not result["top_fractal"].iloc[-1]
        assert not result["bottom_fractal"].iloc[0]
        assert not result["bottom_fractal"].iloc[-1]

    def test_filter_valid_fractals(self):
        from factors.chanlun.fractal import identify_fractals, filter_valid_fractals
        # 构造连续顶分型：只保留更高的
        df = pd.DataFrame({
            "open":  [10, 15, 12, 16, 13],
            "close": [15, 12, 16, 13, 11],
            "high":  [16, 17, 16.5, 18, 14],
            "low":   [9, 11, 10, 12, 8],
        })
        result = filter_valid_fractals(df)
        top_count = result["top_fractal"].sum()
        assert top_count <= 2  # 最多保留两个交替的顶
