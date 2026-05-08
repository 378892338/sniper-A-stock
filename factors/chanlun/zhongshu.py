"""中枢识别 + 买卖点 — 缠论 Sprint 2"""

from dataclasses import dataclass, field
import pandas as pd

from core.logger import get_logger

logger = get_logger("factors.chanlun.zhongshu")

# ── 买点评分（冷启动占位值，纳入回测校准）──
BUY_POINT_SCORES = {
    "一买": 6,
    "二买": 12,
    "类二买": 12,
    "三买": 18,
    "二三买重合": 25,
    "底分型": 3,
    "无买点": 0,
}


@dataclass
class Zhongshu:
    """中枢"""
    ZG: float           # 中枢高点 = min(各笔高点)
    ZD: float           # 中枢低点 = max(各笔低点)
    start_idx: int      # 起始位置
    end_idx: int        # 结束位置
    stroke_count: int = 3
    strokes: list[dict] = field(default_factory=list)


@dataclass
class BuyPoint:
    """买点"""
    type: str       # 一买/二买/三买/类二买/二三买重合
    score: float
    idx: int
    price: float    # 该买点的关键价位（一买=最低价, 二买/三买=收盘价）
    date: object = None


@dataclass
class SellPoint:
    """卖点"""
    type: str       # 一卖/二卖/三卖
    idx: int
    price: float    # 该卖点的关键价位（一卖=最高价, 二卖/三卖=收盘价）
    date: object = None


# ── 中枢识别 ──


def identify_zhongshu(df: pd.DataFrame, min_strokes: int = 3) -> list[Zhongshu]:
    """
    从含笔划分的DataFrame中识别中枢。

    中枢定义: 至少 min_strokes 笔重叠的价格区间
    - ZG = min(各笔的高点)
    - ZD = max(各笔的低点)

    要求: ZG > ZD 才构成有效中枢
    """
    if "stroke_idx" not in df.columns or "stroke_type" not in df.columns:
        return []

    stroke_bounds = _extract_stroke_bounds(df)
    if len(stroke_bounds) < min_strokes:
        return []

    zhongshus = []
    i = 0
    while i <= len(stroke_bounds) - min_strokes:
        window = stroke_bounds[i:i + min_strokes]
        highs = [s["high"] for s in window]
        lows = [s["low"] for s in window]

        ZG_candidate = min(highs)
        ZD_candidate = max(lows)

        if ZG_candidate > ZD_candidate:
            zhongshu = Zhongshu(
                ZG=ZG_candidate,
                ZD=ZD_candidate,
                start_idx=window[0]["start_idx"],
                end_idx=window[-1]["end_idx"],
                stroke_count=min_strokes,
                strokes=list(window),
            )

            # 尝试扩展：看后续笔是否还在中枢内
            j = i + min_strokes
            while j < len(stroke_bounds):
                next_s = stroke_bounds[j]
                if next_s["high"] > ZD_candidate and next_s["low"] < ZG_candidate:
                    ZG_candidate = min(ZG_candidate, next_s["high"])
                    ZD_candidate = max(ZD_candidate, next_s["low"])
                    zhongshu = Zhongshu(
                        ZG=ZG_candidate,
                        ZD=ZD_candidate,
                        start_idx=zhongshu.start_idx,
                        end_idx=next_s["end_idx"],
                        stroke_count=zhongshu.stroke_count + 1,
                        strokes=zhongshu.strokes + [next_s],
                    )
                    j += 1
                else:
                    break

            zhongshus.append(zhongshu)
            i = j
        else:
            i += 1

    return zhongshus


def _extract_stroke_bounds(df: pd.DataFrame) -> list[dict]:
    """提取每笔的边界信息 [{start_idx, end_idx, high, low, type}]"""
    bounds = []
    if len(df) == 0:
        return bounds

    # 跳到第一个有效笔
    start = None
    for i in range(len(df)):
        if df["stroke_idx"].iloc[i] >= 0:
            start = i
            break
    if start is None:
        return bounds

    prev_idx = int(df["stroke_idx"].iloc[start]) if pd.notna(df["stroke_idx"].iloc[start]) else -1
    bound_start = start

    for i in range(start + 1, len(df)):
        cur_idx = df["stroke_idx"].iloc[i]
        if cur_idx != prev_idx:
            bs_val = df["stroke_idx"].iloc[bound_start]
            if pd.notna(bs_val) and int(bs_val) >= 0:
                segment = df.iloc[bound_start:i]
                stype = df["stroke_type"].iloc[bound_start]
                bounds.append({
                    "start_idx": bound_start,
                    "end_idx": i - 1,
                    "high": float(segment["high"].max()),
                    "low": float(segment["low"].min()),
                    "type": stype,
                })
            bound_start = i
        prev_idx = int(cur_idx) if pd.notna(cur_idx) else -1

    # 最后一笔
    if bound_start < len(df):
        val_end = df["stroke_idx"].iloc[bound_start]
        safe_idx_end = int(val_end) if pd.notna(val_end) else -1
        if safe_idx_end >= 0:
            segment = df.iloc[bound_start:]
            stype = df["stroke_type"].iloc[bound_start]
            bounds.append({
                "start_idx": bound_start,
                "end_idx": len(df) - 1,
                "high": float(segment["high"].max()),
                "low": float(segment["low"].min()),
                "type": stype,
            })

    return bounds


def get_latest_zhongshu(df: pd.DataFrame) -> Zhongshu | None:
    """获取最近一个中枢"""
    zhongshus = identify_zhongshu(df)
    return zhongshus[-1] if zhongshus else None


def has_overlap(zs1: Zhongshu, zs2: Zhongshu) -> bool:
    """检测两个中枢是否有重叠（扩张/扩展）"""
    return zs1.ZG > zs2.ZD and zs2.ZG > zs1.ZD


# ── 买点检测 ──


def detect_buy_points(df: pd.DataFrame, dif: pd.Series,
                      dea: pd.Series, hist: pd.Series,
                      zhongshus: list[Zhongshu] | None = None) -> list[BuyPoint]:
    """
    检测日线级别缠论买点。

    买点类型:
    - 一买: 中枢下方 + 底背驰（最近5根bar内）
    - 二买: 一买之后回踩不破一买低点
    - 类二买: 中枢内回踩 + MACD金叉
    - 三买: 中枢上方回踩不破ZG
    - 二三买重合: 二买和三买位置重叠
    """
    from factors.chanlun.divergence import detect_bottom_divergence
    from factors.macd import is_golden_cross

    if zhongshus is None:
        zhongshus = identify_zhongshu(df)
    if not zhongshus:
        return []

    close = df["close"]
    low = df["low"]
    bottom_div = detect_bottom_divergence(df, hist)
    golden_cross = is_golden_cross(dif, dea)

    zs = zhongshus[-1]
    recent_close = close.iloc[-1]
    recent_golden = golden_cross.tail(5).any()

    buy_points = []

    # 一买: 中枢下方 + 底背驰（最近5根bar内）
    below_zd = recent_close < zs.ZD
    recent_div = bottom_div.tail(5).any()
    if below_zd and recent_div:
        # 使用背驰信号点的实际低价（非 tail(10) 盲取）
        div_indices = bottom_div.tail(10)
        if div_indices.any():
            div_idx = div_indices[div_indices].index[-1]
            bp_price = float(df.loc[div_idx, "low"])
        else:
            bp_price = float(low.tail(10).min())
        buy_points.append(BuyPoint(
            type="一买", score=BUY_POINT_SCORES["一买"],
            idx=len(df) - 1,
            price=bp_price,
            date=df.index[-1],
        ))

    # 二买: 一买已确认 + 回踩不破一买最低价
    # 需要一买至少发生在5根bar之前（时间分离）
    first_buy_idx = _find_first_buy_in_history(df, zhongshus, bottom_div, dif)
    if first_buy_idx is not None:
        first_buy_low = float(low.iloc[first_buy_idx])
        recent_low = low.tail(5).min()
        bars_since_first_buy = len(df) - 1 - first_buy_idx
        if bars_since_first_buy >= 3 and recent_low > first_buy_low * 1.01:
            if recent_close < zs.ZG and recent_golden:
                buy_points.append(BuyPoint(
                    type="二买", score=BUY_POINT_SCORES["二买"],
                    idx=len(df) - 1, price=float(recent_close),
                    date=df.index[-1],
                ))

    # 类二买: 中枢内回踩 + MACD金叉
    if zs.ZD <= recent_close <= zs.ZG and recent_golden:
        buy_points.append(BuyPoint(
            type="类二买", score=BUY_POINT_SCORES["类二买"],
            idx=len(df) - 1, price=float(recent_close),
            date=df.index[-1],
        ))

    # 三买: 中枢上方回踩不破ZG
    recent_min = low.tail(20).min()
    if recent_close > zs.ZG and recent_min > zs.ZG * 0.98 and recent_golden:
        three_buy = BuyPoint(
            type="三买", score=BUY_POINT_SCORES["三买"],
            idx=len(df) - 1, price=float(recent_close),
            date=df.index[-1],
        )

        # 二三买重合
        if check_buy_point_overlap(
            current_price=recent_close,
            zhongshu_ZG=zs.ZG,
            zhongshu_ZD=zs.ZD,
            has_second_buy=any(bp.type == "二买" for bp in buy_points),
            has_third_buy=True,
        ):
            three_buy = BuyPoint(
                type="二三买重合",
                score=BUY_POINT_SCORES["二三买重合"],
                idx=len(df) - 1,
                price=float(recent_close),
                date=df.index[-1],
            )

        buy_points.append(three_buy)

    return buy_points


def _find_first_buy_in_history(df: pd.DataFrame, zhongshus: list[Zhongshu],
                                bottom_div: pd.Series, dif: pd.Series) -> int | None:
    """在历史数据中找最近的一买位置（索引）"""
    if not zhongshus:
        return None
    zs = zhongshus[-1]
    close = df["close"]
    # 从尾部往回搜到中枢起点（最长120bar）
    search_end = max(zs.start_idx, len(df) - 120, 0)
    for i in range(len(df) - 5, search_end, -1):
        if close.iloc[i] < zs.ZD and bottom_div.iloc[i]:
            return i
    return None


def check_buy_point_overlap(current_price: float, zhongshu_ZG: float,
                            zhongshu_ZD: float, has_second_buy: bool,
                            has_third_buy: bool) -> bool:
    """检测是否二三买重合（价格在ZG附近3%以内）"""
    has_buy = has_second_buy and has_third_buy
    if not has_buy or zhongshu_ZG <= 0:
        return False
    return abs(current_price - zhongshu_ZG) / zhongshu_ZG < 0.03


def get_best_buy_point(points: list[BuyPoint]) -> BuyPoint | None:
    """取最优买点（评分最高）"""
    if not points:
        return None
    return max(points, key=lambda p: p.score)


def get_latest_buy_point(df: pd.DataFrame, dif: pd.Series,
                         dea: pd.Series, hist: pd.Series,
                         zhongshus: list[Zhongshu] | None = None) -> dict:
    """获取最近买点，返回 {label, score}"""
    points = detect_buy_points(df, dif, dea, hist, zhongshus)
    if not points:
        from factors.chanlun.fractal import identify_fractals
        df_frac = identify_fractals(df)
        recent_bottom = df_frac["bottom_fractal"].tail(10).sum()
        if recent_bottom >= 1:
            return {"label": "底分型", "score": float(BUY_POINT_SCORES["底分型"])}
        return {"label": "无买点", "score": float(BUY_POINT_SCORES["无买点"])}

    best = get_best_buy_point(points)
    return {"label": best.type, "score": float(best.score)}


# ── 卖点检测 ──


def detect_sell_points(df: pd.DataFrame, dif: pd.Series,
                       dea: pd.Series, hist: pd.Series,
                       zhongshus: list[Zhongshu] | None = None) -> list[SellPoint]:
    """
    检测日线级别缠论卖点（买点的镜像）。

    卖点类型:
    - 一卖: 中枢上方 + 顶背驰
    - 二卖: 反弹不破一卖高点
    - 三卖: 中枢下方反弹不进中枢
    """
    from factors.chanlun.divergence import detect_top_divergence
    from factors.macd import is_death_cross

    if zhongshus is None:
        zhongshus = identify_zhongshu(df)
    if not zhongshus:
        return []

    close = df["close"]
    high = df["high"]
    top_div = detect_top_divergence(df, hist)
    death_cross = is_death_cross(dif, dea)

    zs = zhongshus[-1]
    recent_close = close.iloc[-1]
    recent_death = death_cross.tail(5).any()

    sell_points = []

    # 一卖: 中枢上方 + 顶背驰（最近5根bar内）
    above_zg = recent_close > zs.ZG
    recent_div = top_div.tail(5).any()
    if above_zg and recent_div:
        # 使用背驰信号点的实际高价
        div_indices = top_div.tail(10)
        if div_indices.any():
            div_idx = div_indices[div_indices].index[-1]
            sp_price = float(df.loc[div_idx, "high"])
        else:
            sp_price = float(high.tail(10).max())
        sell_points.append(SellPoint(
            type="一卖", idx=len(df) - 1,
            price=sp_price,
            date=df.index[-1],
        ))

    # 二卖: 反弹不破一卖高点
    if sell_points:
        first_sell_high = sell_points[-1].price
        if recent_close < first_sell_high and recent_death:
            sell_points.append(SellPoint(
                type="二卖", idx=len(df) - 1,
                price=float(recent_close), date=df.index[-1],
            ))

    # 三卖: 中枢下方反弹不进中枢
    recent_max = high.tail(20).max()
    if recent_close < zs.ZD and recent_max < zs.ZD and recent_death:
        sell_points.append(SellPoint(
            type="三卖", idx=len(df) - 1,
            price=float(recent_close), date=df.index[-1],
        ))

    return sell_points


def get_best_sell_point(points: list[SellPoint]) -> SellPoint | None:
    """取最优卖点（一卖优先，其次最近）"""
    if not points:
        return None
    for p in points:
        if p.type == "一卖":
            return p
    return points[-1]


def get_latest_sell_point(df: pd.DataFrame, dif: pd.Series,
                          dea: pd.Series, hist: pd.Series,
                          zhongshus: list[Zhongshu] | None = None) -> dict:
    """获取最近卖点"""
    points = detect_sell_points(df, dif, dea, hist, zhongshus)
    if not points:
        return {"label": "无卖点", "score": 0.0}

    best = get_best_sell_point(points)
    return {"label": best.type, "price": best.price,
            "date": str(best.date) if best.date else None}


def score_zhongshu_position_for_trend(df: pd.DataFrame,
                                       zhongshus: list[Zhongshu] | None = None,
                                       bonus_range: float = 3.0) -> float:
    """
    L1/L2 中枢位置对趋势的加成 (返回 -3 ~ +3)。

    价格在中枢上方 → +bonus_range (支撑有效)
    价格在中枢内部 → 0
    价格在中枢下方 → -bonus_range (压力有效)

    用于统一趋势评分中的"稳定性"维度加成。
    """
    if zhongshus is None:
        zhongshus = identify_zhongshu(df)
    if not zhongshus or "close" not in df.columns:
        return 0.0

    zs = zhongshus[-1]
    if zs.ZG <= zs.ZD:
        return 0.0

    price = float(df["close"].iloc[-1])

    if price > zs.ZG:
        return bonus_range
    elif price < zs.ZD:
        return -bonus_range
    return 0.0
