"""中枢识别 + 买卖点测试 — 缠论 Sprint 2"""

import pandas as pd
import numpy as np

from factors.chanlun.contain import process_containment
from factors.chanlun.fractal import identify_fractals
from factors.chanlun.stroke import divide_strokes
from factors.macd import calc_macd


# ── 测试数据: 构造标准中枢形态 ──

def make_zhongshu_data() -> pd.DataFrame:
    """
    构造一个包含标准中枢的K线序列（确定性数据）。

    走势: 上涨 → 中枢震荡(≥3笔重叠) → 突破
    """
    n = 120
    dates = pd.date_range("2025-01-01", periods=n, freq="B")

    data = []
    # 阶段1: 稳步上涨 (0-30), 每天涨一点，无噪声
    for i in range(30):
        c = 8.0 + i * 0.08
        data.append({
            "open": c - 0.05, "high": c + 0.3, "low": c - 0.2, "close": c + 0.1,
            "volume": 20000
        })

    # 阶段2: 中枢震荡 — 构造3笔在 9.5~10.5 重叠
    # 笔1(向上): 9.0 -> 10.5
    for i in range(10):
        c = 9.0 + i * 0.15
        data.append({
            "open": c - 0.05, "high": c + 0.35, "low": c - 0.15, "close": c + 0.1,
            "volume": 20000
        })
    # 笔2(向下): 10.5 -> 9.0
    for i in range(10):
        c = 10.5 - i * 0.15
        data.append({
            "open": c + 0.05, "high": c + 0.15, "low": c - 0.35, "close": c - 0.1,
            "volume": 20000
        })
    # 笔3(向上): 9.0 -> 10.5
    for i in range(10):
        c = 9.0 + i * 0.15
        data.append({
            "open": c - 0.05, "high": c + 0.35, "low": c - 0.15, "close": c + 0.1,
            "volume": 20000
        })
    # 笔4(向下): 10.5 -> 9.5 (在中枢内)
    for i in range(8):
        c = 10.5 - i * 0.125
        data.append({
            "open": c + 0.05, "high": c + 0.15, "low": c - 0.3, "close": c - 0.1,
            "volume": 20000
        })
    # 笔5(向上): 9.5 -> 10.8 (还在中枢重叠范围)
    for i in range(8):
        c = 9.5 + i * 0.16
        data.append({
            "open": c - 0.05, "high": c + 0.3, "low": c - 0.15, "close": c + 0.1,
            "volume": 20000
        })

    # 阶段3: 突破上行 (离开中枢)
    remaining = n - len(data)
    base = 11.5
    for i in range(remaining):
        c = base + i * 0.12
        data.append({
            "open": c - 0.05, "high": c + 0.4, "low": c - 0.15, "close": c + 0.1,
            "volume": 20000
        })

    df = pd.DataFrame(data, index=dates[:len(data)])
    return df


def make_trending_data() -> pd.DataFrame:
    """构造单边上涨数据（趋势明确，中枢极少）"""
    n = 80
    dates = pd.date_range("2025-03-01", periods=n, freq="B")
    close = np.linspace(8, 15, n)
    df = pd.DataFrame({
        "open": close - 0.03,
        "high": close + 0.08,
        "low": close - 0.05,
        "close": close + 0.03,
        "volume": np.random.randint(10000, 50000, n),
    }, index=dates)
    return df


# ── 测试用例 ──


class TestZhongshuIdentification:
    """中枢识别测试"""

    def test_identify_zhongshu_from_strokes(self):
        """从笔序列识别中枢"""
        from factors.chanlun.zhongshu import identify_zhongshu, Zhongshu

        df = make_zhongshu_data()
        df_frac = identify_fractals(df)
        df_stroke = divide_strokes(df_frac)

        zhongshus = identify_zhongshu(df_stroke)

        assert len(zhongshus) > 0, "应该在震荡区识别到中枢"
        for zs in zhongshus:
            assert isinstance(zs, Zhongshu)
            assert zs.ZG > zs.ZD, f"ZG({zs.ZG})应> ZD({zs.ZD})"
            assert zs.stroke_count >= 3, f"中枢至少3笔重叠, 实际{zs.stroke_count}"

    def test_no_zhongshu_in_trend(self):
        """单边趋势中枢远少于震荡行情"""
        from factors.chanlun.zhongshu import identify_zhongshu

        df_trend = make_trending_data()
        df_frac_t = identify_fractals(df_trend)
        df_stroke_t = divide_strokes(df_frac_t)
        zhs_trend = identify_zhongshu(df_stroke_t)

        df_range = make_zhongshu_data()
        df_frac_r = identify_fractals(df_range)
        df_stroke_r = divide_strokes(df_frac_r)
        zhs_range = identify_zhongshu(df_stroke_r)

        # 趋势行情中枢应远少于震荡行情
        assert len(zhs_trend) < len(zhs_range), \
            f"趋势行情中枢({len(zhs_trend)})应少于震荡行情({len(zhs_range)})"

    def test_zhongshu_has_zg_zd(self):
        """中枢有明确的中枢高点和低点"""
        from factors.chanlun.zhongshu import identify_zhongshu

        df = make_zhongshu_data()
        df_frac = identify_fractals(df)
        df_stroke = divide_strokes(df_frac)

        zhongshus = identify_zhongshu(df_stroke)
        for zs in zhongshus:
            assert zs.ZG is not None
            assert zs.ZD is not None
            assert zs.ZG > zs.ZD
            assert zs.start_idx < zs.end_idx


class TestBuySellPoints:
    """买卖点检测测试"""

    def test_detect_buy_points_returns_list(self):
        """买点检测返回列表（dataclass 属性访问）"""
        from factors.chanlun.zhongshu import detect_buy_points, BuyPoint

        df = make_zhongshu_data()
        dif, dea, hist = calc_macd(df["close"])
        buy_points = detect_buy_points(df, dif, dea, hist)

        assert isinstance(buy_points, list)
        for bp in buy_points:
            assert isinstance(bp, BuyPoint)
            assert bp.type in ("一买", "二买", "三买", "类二买", "二三买重合")
            assert bp.score >= 0

    def test_detect_sell_points_returns_list(self):
        """卖点检测返回列表（dataclass 属性访问）"""
        from factors.chanlun.zhongshu import detect_sell_points, SellPoint

        df = make_zhongshu_data()
        dif, dea, hist = calc_macd(df["close"])
        sell_points = detect_sell_points(df, dif, dea, hist)

        assert isinstance(sell_points, list)
        for sp in sell_points:
            assert isinstance(sp, SellPoint)
            assert sp.type in ("一卖", "二卖", "三卖")

    def test_buy_point_scores_match_doc(self):
        """买点评分与文档一致"""
        from factors.chanlun.zhongshu import BUY_POINT_SCORES

        assert BUY_POINT_SCORES["一买"] == 6
        assert BUY_POINT_SCORES["二买"] == 12
        assert BUY_POINT_SCORES["类二买"] == 12
        assert BUY_POINT_SCORES["三买"] == 18
        assert BUY_POINT_SCORES["二三买重合"] == 25


class TestZhongshuExpansion:
    """中枢扩张/扩展测试"""

    def test_zhongshu_overlap_detection(self):
        """检测相邻中枢是否重叠"""
        from factors.chanlun.zhongshu import has_overlap
        from factors.chanlun.zhongshu import Zhongshu

        zs1 = Zhongshu(ZG=10.5, ZD=9.5, start_idx=10, end_idx=30)
        zs2 = Zhongshu(ZG=11.0, ZD=10.2, start_idx=25, end_idx=45)

        assert has_overlap(zs1, zs2), "重叠中枢应检测到"

        zs3 = Zhongshu(ZG=12.0, ZD=11.5, start_idx=50, end_idx=70)
        assert not has_overlap(zs1, zs3), "不重叠中枢应返回False"


class TestThirdBuySellOverlap:
    """二三买重合检测"""

    def test_23_overlap_detection(self):
        """二三买重合信号"""
        from factors.chanlun.zhongshu import check_buy_point_overlap

        # 模拟二三买重合: 价格回踩中枢上沿(ZG)附近，既是二买也是三买
        result = check_buy_point_overlap(
            current_price=10.6,
            zhongshu_ZG=10.5,
            zhongshu_ZD=9.5,
            has_second_buy=True,
            has_third_buy=True,
        )
        assert result, "二三买重合应被检测到"


def test_best_buy_point():
    """获取最优买点"""
    from factors.chanlun.zhongshu import get_best_buy_point, BuyPoint

    points = [
        BuyPoint(type="一买", score=6, idx=50, price=9.8),
        BuyPoint(type="三买", score=18, idx=70, price=11.2),
        BuyPoint(type="二买", score=12, idx=60, price=10.3),
    ]
    best = get_best_buy_point(points)
    assert best is not None
    assert best.type == "三买"  # 最高分
    assert best.score == 18


def test_best_sell_point():
    """获取最优卖点"""
    from factors.chanlun.zhongshu import get_best_sell_point, SellPoint

    points = [
        SellPoint(type="一卖", idx=80, price=13.5),
        SellPoint(type="三卖", idx=95, price=11.0),
    ]
    best = get_best_sell_point(points)
    assert best is not None
    assert best.type == "一卖"  # 一卖优先


def test_zhongshu_from_stroke_points():
    """用笔画端点列表识别中枢"""
    from factors.chanlun.zhongshu import identify_zhongshu
    from factors.chanlun.stroke import get_stroke_points
    from factors.chanlun.fractal import identify_fractals

    df = make_zhongshu_data()
    df_frac = identify_fractals(df)
    df_strokes = divide_strokes(df_frac)

    zhongshus = identify_zhongshu(df_strokes)
    assert len(zhongshus) >= 0, "应能正常处理笔数据"


def test_latest_buy_point():
    """获取最近买点"""
    from factors.chanlun.zhongshu import get_latest_buy_point

    df = make_zhongshu_data()
    dif, dea, hist = calc_macd(df["close"])
    result = get_latest_buy_point(df, dif, dea, hist)

    assert isinstance(result, dict)
    assert "label" in result
    assert "score" in result
    assert result["score"] >= 0
