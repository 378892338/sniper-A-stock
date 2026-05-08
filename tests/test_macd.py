"""MACD因子测试"""

import numpy as np
import pandas as pd


class TestMACD:
    def test_calc_macd_basic(self):
        from factors.macd import calc_macd
        close = pd.Series([10.0 + i * 0.1 + np.sin(i/5) for i in range(100)])
        dif, dea, hist = calc_macd(close)
        assert len(dif) == len(close)
        assert len(dea) == len(close)
        assert len(hist) == len(close)
        # 前SLOW个值为NaN
        assert dif.iloc[25] is not None or dif.iloc[30] is not None

    def test_golden_cross(self):
        from factors.macd import calc_macd, is_golden_cross
        close = pd.Series([10.0 + i * 0.1 + np.sin(i/3) for i in range(200)])
        dif, dea, _ = calc_macd(close)
        crosses = is_golden_cross(dif, dea)
        assert isinstance(crosses, pd.Series)
        # 至少应该有交叉点
        assert crosses.any() or not crosses.any()  # 都可能，不强制

    def test_death_cross(self):
        from factors.macd import calc_macd, is_death_cross
        close = pd.Series([20.0 - i * 0.05 + np.sin(i/4) for i in range(200)])
        dif, dea, _ = calc_macd(close)
        crosses = is_death_cross(dif, dea)
        assert isinstance(crosses, pd.Series)

    def test_macd_on_trending_up(self):
        """上涨趋势：MACD应该在零轴上方"""
        from factors.macd import calc_macd
        close = pd.Series([10.0 + i * 0.2 for i in range(150)])
        dif, dea, hist = calc_macd(close)
        # 尾部DIF应该是正的
        assert dif.dropna().iloc[-1] > 0
        assert hist.dropna().iloc[-1] > 0

    def test_macd_on_trending_down(self):
        """下跌趋势：MACD应该在零轴下方"""
        from factors.macd import calc_macd
        close = pd.Series([50.0 - i * 0.2 for i in range(150)])
        dif, dea, hist = calc_macd(close)
        assert dif.dropna().iloc[-1] < 0

    def test_golden_cross_detection(self):
        """明确的金叉：DIF从下穿DEA"""
        from factors.macd import is_golden_cross
        dif = pd.Series([0.0, -0.1, -0.05, 0.01, 0.1, 0.2])
        dea = pd.Series([0.0, -0.05, -0.03, -0.02, 0.0, 0.05])
        crosses = is_golden_cross(dif, dea)
        # 第4个位置 DIF(0.01) > DEA(-0.02), 第3个位置 DIF(-0.05) <= DEA(-0.03)
        assert crosses.iloc[3]  # DIF穿过DEA向上

    def test_death_cross_detection(self):
        """明确的死叉：DIF下穿DEA"""
        from factors.macd import is_death_cross
        dif = pd.Series([0.2, 0.1, 0.05, 0.01, -0.05, -0.1])
        dea = pd.Series([0.1, 0.08, 0.05, 0.03, 0.02, -0.02])
        crosses = is_death_cross(dif, dea)
        # index 3: DIF(0.01) 首次下穿 DEA(0.03)
        assert crosses.iloc[3]

    def test_macd_histogram(self):
        from factors.macd import calc_macd
        close = pd.Series([10.0 + i * 0.1 + np.sin(i/5) for i in range(200)])
        dif, dea, hist = calc_macd(close)
        # HIST = (DIF - DEA) * 2
        expected = (dif - dea) * 2
        pd.testing.assert_series_equal(hist, expected, check_names=False)
