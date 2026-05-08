"""因子计算模块单元测试"""

import pandas as pd
import numpy as np
import pytest

from models.factors import (
    calc_ma, calc_macd, calc_amplitude, calc_vol_ratio,
    is_limit_up, is_breakout_candle,
    calc_trend_layer, detect_ma_converge, detect_bottom_reversal,
    calc_timing_score, calc_all_factors, safe_expanding_normalize,
)


@pytest.fixture
def sample_df():
    """生成200天模拟日线数据（上升趋势 + 震荡）"""
    np.random.seed(42)
    n = 200
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    trend = np.linspace(10, 15, n)
    noise = np.random.randn(n) * 0.2
    close = trend + noise

    df = pd.DataFrame({
        "open": close - np.abs(np.random.randn(n)) * 0.1,
        "high": close + np.abs(np.random.randn(n)) * 0.2,
        "low": close - np.abs(np.random.randn(n)) * 0.2,
        "close": close,
        "volume": np.random.randint(100000, 1000000, n).astype(float),
    }, index=dates)
    df["turnover"] = np.random.uniform(0.01, 0.05, n)
    return df


class TestTechnicalIndicators:
    def test_calc_ma(self, sample_df):
        ma = calc_ma(sample_df["close"], 20)
        assert len(ma) == len(sample_df)
        assert ma.iloc[19] == pytest.approx(sample_df["close"].iloc[:20].mean(), rel=0.01)
        assert pd.isna(ma.iloc[0])

    def test_calc_macd(self, sample_df):
        dif, dea, hist = calc_macd(sample_df["close"])
        assert len(dif) == len(sample_df)
        assert len(dea) == len(sample_df)
        assert len(hist) == len(sample_df)

    def test_calc_amplitude(self, sample_df):
        amp = calc_amplitude(sample_df, 60)
        assert len(amp) == len(sample_df)
        assert all(v >= 0 or pd.isna(v) for v in amp)

    def test_calc_vol_ratio(self, sample_df):
        ratio = calc_vol_ratio(sample_df["volume"], 20)
        assert len(ratio) == len(sample_df)

    def test_is_limit_up_main_board(self, sample_df):
        df = sample_df.copy()
        df["open"] = df["close"] * 0.93  # ~7.5%涨幅，低于9.8%阈值
        df["high"] = df["close"] * 1.02  # high > close 即没封板
        result = is_limit_up(df["close"], df["open"], df["high"], "600001")
        assert result.sum() == 0  # 未达涨停阈值且未封板

    def test_is_limit_up_cyb(self, sample_df):
        df = sample_df.copy()
        # 创业板模拟涨停封板
        df["open"] = 10.0
        df["close"] = 12.0
        df["high"] = 12.0
        result = is_limit_up(df["close"], df["open"], df["high"], "300001")
        assert result.sum() > 0

    def test_is_breakout_candle(self, sample_df):
        df = sample_df.copy()
        df["close"] = df["open"] * 1.04  # 4%涨幅
        result = is_breakout_candle(df["close"], df["open"])
        assert result.all()


class TestTrendLayer:
    def test_calc_trend_layer(self, sample_df):
        df = calc_trend_layer(sample_df)
        assert "ma20" in df.columns
        assert "ma60" in df.columns
        assert "ma250" in df.columns
        assert "trend_pass" in df.columns

    def test_calc_trend_layer_strong_uptrend(self):
        """强上升趋势中MA20>MA60且价格在年线上方（需要>250天数据）"""
        n = 300
        dates = pd.date_range("2020-01-01", periods=n, freq="B")
        close = 10 + np.linspace(0, 20, n)  # 长期上升
        df = pd.DataFrame({
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": np.ones(n) * 1e6,
        }, index=dates)
        result = calc_trend_layer(df)
        # 后期(第260天之后) MA20 > MA60 且 close > MA250
        recent = result.iloc[-30:]
        assert recent["trend_pass"].any()


class TestStructureLayer:
    def test_detect_ma_converge_no_signal_on_trend(self, sample_df):
        from config.settings import STRUCTURE_CONFIG
        cfg = {"ma_converge": STRUCTURE_CONFIG["ma_converge"]}
        # 趋势行情中MA发散，不应产生粘合信号
        signal = detect_ma_converge(sample_df, cfg["ma_converge"])
        # 持续上升趋势中均线发散，粘合信号应该很少
        assert signal.sum() < 20  # 最多10%的日子有信号

    def test_detect_bottom_reversal(self, sample_df):
        from config.settings import STRUCTURE_CONFIG
        cfg = {"bottom_reversal": STRUCTURE_CONFIG["bottom_reversal"]}
        signal = detect_bottom_reversal(sample_df, cfg["bottom_reversal"])
        assert len(signal) == len(sample_df)
        assert signal.sum() >= 0


class TestTimingLayer:
    def test_calc_timing_score(self, sample_df):
        from config.settings import TIMING_CONFIG
        score = calc_timing_score(sample_df, TIMING_CONFIG)
        assert len(score) == len(sample_df)
        assert score.max() <= 1.0  # 最高分1.0 (两个0.5相加)
        assert score.min() >= 0.0


class TestExpandingNormalize:
    def test_no_future_leakage(self):
        """验证 expanding 归一化不会前视泄露"""
        s = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0])
        result = safe_expanding_normalize(s)
        # 第0个值: 1/1*100 = 100
        assert result.iloc[0] == 100.0
        # 第2个值: 3/3*100 = 100
        assert result.iloc[2] == 100.0
        # 第3个值: 2/3*100 ≈ 66.7 (最大值仍是第2个的3.0)
        assert result.iloc[3] == pytest.approx(66.67, rel=0.05)

    def test_handles_zeros(self):
        s = pd.Series([0.0, 0.0, 1.0, 0.0])
        result = safe_expanding_normalize(s)
        assert result.iloc[0] == 0.0
        assert result.iloc[2] == 100.0


class TestCalcAllFactors:
    def test_calc_all_factors_returns_all_scores(self, sample_df):
        df = calc_all_factors(sample_df, symbol="600001")
        required = ["score_technical", "score_capital", "score_fundamental", "score_total"]
        for col in required:
            assert col in df.columns, f"缺少列: {col}"
        assert len(df) == len(sample_df)

    def test_calc_all_factors_no_future_leak(self, sample_df):
        """验证 calc_all_factors 不使用未来数据"""
        half = len(sample_df) // 2
        df_full = calc_all_factors(sample_df.copy(), symbol="600001")
        df_half = calc_all_factors(sample_df.iloc[:half].copy(), symbol="600001")

        # 前半段的结果应该一致
        for col in ["score_technical", "score_capital", "score_fundamental"]:
            full_vals = df_full[col].iloc[:half].values
            half_vals = df_half[col].iloc[:half].values
            # 允许 masked array 转换差异
            diff = np.abs(full_vals - half_vals)
            assert np.nanmax(diff) < 1.0, f"{col} 差异过大: max={np.nanmax(diff):.4f}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
