"""笔的划分测试"""

import numpy as np
import pandas as pd


class TestStroke:
    def test_divide_strokes_basic(self):
        from factors.chanlun.stroke import divide_strokes
        # 构造顶底交替的数据
        n = 60
        prices = []
        for i in range(n):
            prices.append(10 + 5 * np.sin(i / 10) + i * 0.05)
        close = pd.Series(prices)
        df = pd.DataFrame({
            "open": close - 0.1,
            "close": close,
            "high": close + abs(np.random.randn(n) * 0.3),
            "low": close - abs(np.random.randn(n) * 0.3),
        })
        result = divide_strokes(df)
        assert "stroke_type" in result.columns
        assert "stroke_idx" in result.columns

    def test_get_stroke_points(self):
        from factors.chanlun.stroke import get_stroke_points
        n = 80
        prices = [10 + 3 * np.sin(i / 8) + i * 0.02 for i in range(n)]
        df = pd.DataFrame({
            "open":  [p - 0.05 for p in prices],
            "close": [p + 0.05 for p in prices],
            "high":  [p + 0.3 for p in prices],
            "low":   [p - 0.3 for p in prices],
        })
        points = get_stroke_points(df)
        # 至少有顶底交替
        assert len(points) >= 1
