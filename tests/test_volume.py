"""量能分析测试"""

import numpy as np
import pandas as pd


class TestVolume:
    def test_calc_volume_ma(self):
        from factors.volume import calc_volume_ma
        vol = pd.Series([100.0 + i for i in range(30)])
        ma = calc_volume_ma(vol, 5)
        assert len(ma) == 30
        assert not np.isnan(ma.iloc[4])

    def test_calc_volume_ratio(self):
        from factors.volume import calc_volume_ratio
        vol = pd.Series([100.0] * 10 + [200.0])  # 突然放量
        ratio = calc_volume_ratio(vol, 5)
        assert ratio.iloc[-1] > 1.5

    def test_is_volume_expanding(self):
        from factors.volume import is_volume_expanding
        vol = pd.Series([100.0] * 15 + [300.0])
        expanding = is_volume_expanding(vol, 5)
        assert expanding.iloc[-1]

    def test_classify_volume_price(self):
        from factors.volume import classify_volume_price
        # 放量上涨
        vol = pd.Series([100.0] * 10 + [300.0])
        close = pd.Series([10.0 + i * 0.01 for i in range(10)] + [10.1 + 0.1])
        vp = classify_volume_price(vol, close)
        assert vp.iloc[-1] == "放量上涨"

    def test_volume_health_score(self):
        from factors.volume import score_volume_health
        vol = pd.Series([100.0 + abs(np.sin(i)) * 50 for i in range(30)])
        close = pd.Series([10.0 + i * 0.05 + np.sin(i) for i in range(30)])
        score = score_volume_health(vol, close)
        assert 0 <= score <= 100

    def test_is_amount_trending_up(self):
        from factors.volume import is_amount_trending_up
        amount = pd.Series([10000 + i * 100 for i in range(30)])
        assert is_amount_trending_up(amount)
        amount_down = pd.Series([30000 - i * 100 for i in range(30)])
        assert not is_amount_trending_up(amount_down)
