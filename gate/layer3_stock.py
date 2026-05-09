"""第三层门卫：个股筛选 — Gate 3取2 + 多因子评分

Gate（3取2）:
  B1. 周线MACD: DIF>DEA 且 柱状图为正
  B2. 月线MACD: 金叉 OR 零轴上 OR 底背离+DIF拐头
  B3. 资金面: 不反向流出

顶部拦截: T1(顶背驰)×3 + T2(周线死叉)×2 + T3(日线死叉/DIF<0)×1 ≥ 4 → 拦截

评分: 由 multi_factor 模块批量计算（截面标准化），assess_stock 接收预计算分数。
"""

from dataclasses import dataclass, field

import pandas as pd

from factors.macd import (
    calc_macd, is_golden_cross, is_dif_above_zero, is_dif_turning_up,
    is_death_cross, check_daily_macd_above_ma20,
)
from factors.chanlun.divergence import (
    check_weekly_top_divergence, check_daily_top_divergence,
    check_monthly_bottom_divergence,
)
from factors.chanlun.zhongshu import get_latest_sell_point
from factors.volume import score_volume_three_part
from gate.fund_fallback import assess_fund_for_layer
from core.logger import get_logger

logger = get_logger("gate.layer3")


@dataclass
class StockVerdict:
    """个股评估结果"""
    symbol: str
    classification: str  # "强势" / "一般" / "不符合"
    passed_gate: bool  # 硬门槛全过
    score: float = 0.0  # 过门后评分 (0-100) — 由 multi_factor 预计算
    trend_score: float = 0.0
    alpha_score: float = 0.0
    risk_score: float = 0.0
    fund_level: str = "L1"
    fund_confidence: float = 1.0
    fund_note: str = ""
    gate_details: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    etf_tags: list[str] = field(default_factory=list)  # ETF归属标记
    bullish_score: int = 0   # 看多条件命中数 (3取2)
    bearish_score: int = 0   # 顶部信号命中数 (0-3)
    risk_warning: bool = False  # 1个顶部信号 → 减仓，≥2 → 清仓
    pattern_mult: float = 1.0  # 形态乘数 (0.90-1.20), 由 _calc_pattern_multiplier 计算
    chan_buy_point: str = ""   # 缠论买点类型 (一买/二买/三买)
    chan_buy_score: float = 0.0  # 缠论买点评分
    pattern_labels: list[str] = field(default_factory=list)  # §10: 检测到的形态标签
    pattern_category: str = ""  # §10: 突破型/反转型/均线型/无形态


def assess_stock(
    symbol: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame | None = None,
    monthly_df: pd.DataFrame | None = None,
    fund_data: dict | None = None,
    factor_scores: dict | None = None,
    precomputed_macd: dict | None = None,
    fast_mode: bool = False,
) -> StockVerdict:
    """评估单个股票。

    Gate（3取2）:
      B1. 周线MACD: DIF>DEA 且 柱状图为正
      B2. 月线MACD: 金叉 OR 零轴上 OR 底背离+DIF拐头
      B3. 资金面: 不反向流出

    顶部拦截（不变）:
      T1. 日线顶背驰 OR 周线顶背驰 (×3)
      T2. 周线死叉 近8周 (×2)
      T3. 日线死叉 or 日线DIF<0 (×1)
      加权 ≥ 4 → 拦截

    评分: 优先使用 factor_scores（multi_factor 预计算），
          无 factor_scores 时退化为 MACD 评分。

    factor_scores: {"score": 65.2, "trend_score": 18.0, "alpha_score": 28.0, "risk_score": 19.2}
    """
    gate = {}
    warnings = []

    # 优先使用预计算的分型数据，避免重复 identify_fractals
    from factors.chanlun.fractal import identify_fractals
    if precomputed_macd is not None and precomputed_macd.get("daily_top_fractal") is not None:
        if "top_fractal" not in daily_df.columns:
            daily_df = daily_df.copy()
            daily_df["top_fractal"] = precomputed_macd["daily_top_fractal"]
            daily_df["bottom_fractal"] = precomputed_macd["daily_bottom_fractal"]
    elif "top_fractal" not in daily_df.columns:
        daily_df = identify_fractals(daily_df)
    if weekly_df is not None and not weekly_df.empty:
        if precomputed_macd is not None and precomputed_macd.get("weekly_top_fractal") is not None:
            if "top_fractal" not in weekly_df.columns:
                weekly_df = weekly_df.copy()
                weekly_df["top_fractal"] = precomputed_macd["weekly_top_fractal"]
                weekly_df["bottom_fractal"] = precomputed_macd["weekly_bottom_fractal"]
        elif "top_fractal" not in weekly_df.columns:
            weekly_df = identify_fractals(weekly_df)

    # 优先使用预计算的 MACD，否则重新计算
    if precomputed_macd is not None:
        daily_dif = precomputed_macd.get("daily_dif")
        daily_dea = precomputed_macd.get("daily_dea")
        daily_hist = precomputed_macd.get("daily_hist")
        weekly_dif = precomputed_macd.get("weekly_dif")
        weekly_dea = precomputed_macd.get("weekly_dea")
        weekly_hist = precomputed_macd.get("weekly_hist")
        monthly_dif = precomputed_macd.get("monthly_dif")
        monthly_dea = precomputed_macd.get("monthly_dea")
        monthly_hist = precomputed_macd.get("monthly_hist")
    else:
        daily_dif, daily_dea, daily_hist = calc_macd(daily_df["close"])
        weekly_dif, weekly_dea, weekly_hist = (
            calc_macd(weekly_df["close"]) if weekly_df is not None and not weekly_df.empty
            else (None, None, None)
        )
        monthly_dif, monthly_dea, monthly_hist = (
            calc_macd(monthly_df["close"]) if monthly_df is not None and not monthly_df.empty
            else (None, None, None)
        )

    # ── Gate: 底部共振（3取2）──
    # B1: 周线MACD金叉状态
    b1 = False
    if weekly_dif is not None and weekly_dea is not None and weekly_hist is not None:
        w_dif_above = bool((weekly_dif > weekly_dea).iloc[-1])
        w_hist_pos = bool((weekly_hist > 0).iloc[-1])
        b1 = w_dif_above and w_hist_pos
        gate["周线MACD_DIF>DEA"] = w_dif_above
        gate["周线MACD_柱状图为正"] = w_hist_pos
    else:
        gate["周线MACD"] = "数据缺失"

    # B2: 月线MACD (保留底背离作为月线过门条件)
    b2 = False
    if monthly_dif is not None and monthly_dea is not None:
        mg = bool(is_golden_cross(monthly_dif, monthly_dea).tail(6).any())
        md0 = bool(is_dif_above_zero(monthly_dif).iloc[-1])
        mdt = bool(is_dif_turning_up(monthly_dif).iloc[-1])
        monthly_bottom_div = check_monthly_bottom_divergence(monthly_df, monthly_dif)
        b2 = mg or md0 or (monthly_bottom_div and mdt)
        gate["月线MACD_金叉"] = mg
        gate["月线MACD_零轴上"] = md0
        gate["月线MACD_拐头"] = mdt
        gate["月线MACD_底背离"] = monthly_bottom_div
    else:
        gate["月线MACD"] = "数据缺失"

    # B3: 资金面 (原来的 B4)
    fund_result = _assess_stock_fund(fund_data, daily_df)
    b3 = fund_result["fund_pass"]
    gate["资金面"] = f"{fund_result['fund_level']} {fund_result['fund_note']}"

    bullish_score = sum([b1, b2, b3])
    gate["底部得分(3取2)"] = bullish_score

    # ── 顶部拦截：日线/周线顶背驰分离加权 (H3) ──
    daily_top_div = check_daily_top_divergence(daily_df, daily_hist)
    weekly_top_div = False
    if weekly_df is not None and not weekly_df.empty and weekly_hist is not None:
        weekly_top_div = check_weekly_top_divergence(weekly_df, weekly_hist)
    gate["日线顶背驰"] = daily_top_div
    gate["周线顶背驰"] = weekly_top_div

    t2 = False
    if weekly_dif is not None and weekly_dea is not None:
        t2 = bool(is_death_cross(weekly_dif, weekly_dea).tail(8).any())
    gate["周线死叉(近8周)"] = t2

    t3 = False
    daily_death = bool(is_death_cross(daily_dif, daily_dea).tail(5).any())
    daily_dif_below_zero = bool((daily_dif < 0).iloc[-1])
    t3 = daily_death or daily_dif_below_zero
    gate["日线死叉"] = daily_death
    gate["日线DIF<0"] = daily_dif_below_zero

    # 顶背驰分离计数：t1_daily + t1_weekly 替代原合并的 t1
    bearish_score = sum([daily_top_div, weekly_top_div, t2, t3])
    gate["顶部得分"] = bearish_score

    # 看空加权：日线顶背驰×2 + 周线顶背驰×3 + 周线死叉×2 + 日线死叉/DIF<0×1
    bearish_weighted = (
        (1 if daily_top_div else 0) * 2 +
        (1 if weekly_top_div else 0) * 3 +
        (1 if t2 else 0) * 2 +
        (1 if t3 else 0) * 1
    )
    gate["看空加权"] = bearish_weighted

    # ── 判定 ──
    if bearish_weighted >= 5:
        all_pass = False
        risk_warning = False
    elif bullish_score >= 2:
        all_pass = True
        risk_warning = bearish_score == 1
    elif bearish_score == 1:
        all_pass = True
        risk_warning = True
    else:
        all_pass = True
        risk_warning = False

    # ── 评分 ──
    score = 0.0
    trend_score = 0.0
    alpha_score = 0.0
    risk_score = 0.0
    pattern_mult = 1.0

    if all_pass:
        if factor_scores is not None:
            score = factor_scores.get("score", 0.0)
            trend_score = factor_scores.get("trend_score", 0.0)
            alpha_score = factor_scores.get("alpha_score", 0.0)
            risk_score = factor_scores.get("risk_score", 0.0)
        else:
            # 退化评分：用原有 MACD+量价 快速评估
            score, trend_score, alpha_score, risk_score = _fallback_score(
                daily_df, weekly_df, weekly_dif, weekly_dea, weekly_hist,
                fund_result,
            )

        # ── 形态分类标签 (§10) ──
        if fast_mode:
            pattern_labels, pattern_category = [], "回测"
        else:
            pattern_labels, pattern_category = _classify_patterns(
                daily_df, weekly_dif, weekly_dea, weekly_hist,
            )

        # ── 形态加成（乘性 0.90x-1.20x）──
        # 仅作用于 Alpha 维度 (D5: pattern_mult 不应用于 trend/risk)
        if factor_scores is not None and "_pattern_mult" in factor_scores:
            pattern_mult = factor_scores["_pattern_mult"]
        elif fast_mode:
            pattern_mult = 1.0  # 回测模式跳过昂贵的形态检测
        else:
            pattern_mult = _calc_pattern_multiplier(daily_df, weekly_dif, weekly_dea, weekly_hist)
        alpha_adjusted = alpha_score * pattern_mult
        score = round(trend_score + alpha_adjusted + risk_score, 1)
        alpha_score = round(alpha_adjusted, 1)
        gate["形态乘数"] = round(pattern_mult, 3)

        # 风险提示
        if fund_result["fund_confidence"] < 1.0:
            warnings.append(f"资金面折扣 ×{fund_result['fund_confidence']}")
        if risk_warning:
            warnings.append("顶部信号=1 → 建议减仓")
        if daily_top_div:
            warnings.append("日线顶背驰风险")
        if weekly_top_div:
            warnings.append("周线顶背驰风险")

        classification = "强势" if score >= 50 else "一般"
    else:
        classification = "不符合"
        if bearish_weighted >= 4:
            warnings.append(f"看空加权={bearish_weighted} → 拦截")
        elif bearish_score >= 2:
            warnings.append(f"顶部得分={bearish_score} → 建议清仓")

    return StockVerdict(
        symbol=symbol,
        classification=classification,
        passed_gate=all_pass,
        score=score,
        trend_score=trend_score,
        alpha_score=alpha_score,
        risk_score=risk_score,
        fund_level=fund_result["fund_level"],
        fund_confidence=fund_result["fund_confidence"],
        fund_note=fund_result["fund_note"],
        gate_details=gate,
        warnings=warnings,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        risk_warning=risk_warning,
        pattern_mult=pattern_mult,
        pattern_labels=pattern_labels,
        pattern_category=pattern_category,
    )


def _classify_patterns(
    daily_df: pd.DataFrame,
    weekly_dif: pd.Series | None = None,
    weekly_dea: pd.Series | None = None,
    weekly_hist: pd.Series | None = None,
) -> tuple[list[str], str]:
    """检测所有形态并归类到 3 个类别 (§10)。

    返回: (pattern_labels, pattern_category)
    - 突破型: 涨停突破, 平台突破, 旗形突破
    - 反转型: W底, 底背驰, 底分型, 缠论买点
    - 均线型: 均线粘合
    """
    labels: list[str] = []

    # 突破型
    try:
        from factors.pattern import (
            detect_limit_up_breakout, detect_platform_breakout,
            detect_flag_triangle_breakout,
        )
        if detect_limit_up_breakout(daily_df).tail(60).any():
            labels.append("涨停突破")
        if detect_platform_breakout(daily_df).tail(20).any():
            labels.append("平台突破")
        if detect_flag_triangle_breakout(daily_df).tail(20).any():
            labels.append("旗形突破")
    except Exception:
        pass

    # 反转型
    try:
        from factors.pattern import detect_double_bottom, detect_ma_convergence
        from factors.macd import calc_macd
        from factors.chanlun.divergence import check_daily_bottom_divergence
        from factors.chanlun.fractal import identify_fractals

        if detect_double_bottom(daily_df).tail(30).any():
            labels.append("W底")

        _, _, daily_hist = calc_macd(daily_df["close"])
        if check_daily_bottom_divergence(daily_df, daily_hist):
            labels.append("底背驰")

        try:
            frac_df = identify_fractals(daily_df.copy())
            if "bottom_fractal" in frac_df.columns and frac_df["bottom_fractal"].tail(10).any():
                labels.append("底分型")
        except Exception:
            pass

        # 缠论买点
        if weekly_dif is not None and weekly_dea is not None and weekly_hist is not None:
            try:
                from factors.chanlun.zhongshu import detect_buy_points, identify_zhongshu
                zhongshus = identify_zhongshu(daily_df)
                buy_points = detect_buy_points(daily_df, weekly_dif, weekly_dea, weekly_hist, zhongshus)
                if buy_points:
                    labels.append("缠论买点")
            except Exception:
                pass
    except Exception:
        pass

    # 均线型
    try:
        from factors.pattern import detect_ma_convergence
        if detect_ma_convergence(daily_df).tail(20).any():
            labels.append("均线粘合")
    except Exception:
        pass

    # 确定主类别
    breakthrough = {"涨停突破", "平台突破", "旗形突破"}
    reversal = {"W底", "底背驰", "底分型", "缠论买点"}
    avg = {"均线粘合"}

    category = "无形态"
    label_set = set(labels)
    if label_set & breakthrough:
        category = "突破型"
    elif label_set & reversal:
        category = "反转型"
    elif label_set & avg:
        category = "均线型"

    if not labels:
        labels.append("无形态")

    return labels, category


def _calc_pattern_multiplier(
    daily_df: pd.DataFrame,
    weekly_dif: pd.Series | None = None,
    weekly_dea: pd.Series | None = None,
    weekly_hist: pd.Series | None = None,
) -> float:
    """
    计算 L3 形态乘性加成 (0.90x-1.20x)。

    有清晰形态的股票获得乘性提升，无形态的股票轻微折扣。
    - 涨停突破 + 经典形态 + 缠论买点 = 形态强度 (0-1)
    - multiplier = 0.90 + 形态强度 × 0.30

    R4: 形态乘数为人工规则，不参与动态权重校准。
    """
    pattern_strength = 0.0

    # A. 涨停突破 (贡献 0-0.3)
    try:
        from factors.pattern import detect_limit_up_breakout
        lu = detect_limit_up_breakout(daily_df)
        if lu.tail(20).any():
            pattern_strength += 0.30
        elif lu.tail(60).any():
            pattern_strength += 0.15
    except Exception as e:
        logger.warning(f"涨停突破检测失败: {e}")

    # B. 经典形态 (贡献 0-0.3)
    try:
        from factors.pattern import score_breakout_patterns
        p_scores = score_breakout_patterns(daily_df)
        classic = {k: v for k, v in p_scores.items() if k != "涨停突破"}
        if classic:
            top2 = sorted(classic.values(), reverse=True)[:2]
            classic_avg = sum(top2) / 2  # 0-1
            pattern_strength += classic_avg * 0.30
    except Exception as e:
        logger.warning(f"经典形态检测失败: {e}")

    # C. 缠论买点 (贡献 0-0.4)
    try:
        from factors.chanlun.zhongshu import detect_buy_points, identify_zhongshu
        from factors.macd import calc_macd

        if weekly_dif is not None and weekly_dea is not None and weekly_hist is not None:
            zhongshus = identify_zhongshu(daily_df)
            buy_points = detect_buy_points(daily_df, weekly_dif, weekly_dea, weekly_hist, zhongshus)
            if buy_points:
                # 归一化: BUY_POINT_SCORES 范围 6-25, 映射到 0-0.4
                best = max(bp.score for bp in buy_points)
                chanlun_strength = min(0.40, best / 25 * 0.40)
                pattern_strength += chanlun_strength
    except Exception as e:
        logger.warning(f"缠论买点检测失败: {e}")

    return round(0.90 + min(0.30, pattern_strength), 3)


def _fallback_score(
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame | None,
    weekly_dif, weekly_dea, weekly_hist,
    fund_result: dict,
) -> tuple[float, float, float, float]:
    """无 multi_factor 预计算时的退化评分。"""
    trend_base = 18.0
    if weekly_dif is not None and weekly_dea is not None and weekly_hist is not None and weekly_df is not None:
        try:
            from gate.layer2_sector import _score_trend_5dim
            trend_base = _score_trend_5dim(weekly_dif, weekly_dea, weekly_hist, weekly_df["close"])
        except Exception as e:
            logger.warning(f"趋势评分回退失败: {e}")

    monthly_ret = daily_df["close"].iloc[-1] / daily_df["close"].iloc[0] - 1 if len(daily_df) > 1 else 0
    mom_bonus = max(0, min(15, monthly_ret * 100))
    hl_range = daily_df["high"].max() - daily_df["low"].min()
    range_pos = (daily_df["close"].iloc[-1] - daily_df["low"].min()) / hl_range if hl_range > 0 else 0.5
    alpha_base = min(25.0, mom_bonus + range_pos * 10)

    vol_base = score_volume_three_part(daily_df["volume"], daily_df["close"]) * 25 / 100 if "volume" in daily_df.columns else 12.5

    fund_base = fund_result["fund_score"]

    score = trend_base + alpha_base + vol_base + fund_base
    return round(max(0.0, min(100.0, score)), 1), trend_base, alpha_base, vol_base


def _assess_stock_fund(fund_data: dict | None, daily_df: pd.DataFrame | None = None) -> dict:
    """评估个股资金面，使用统一评估函数"""
    if fund_data is None:
        return assess_fund_for_layer(for_layer=3, volume_health=50.0, trend_up=False)

    vol_health = 50.0
    trend_up = False
    if daily_df is not None and not daily_df.empty and len(daily_df) >= 20:
        vol_health = score_volume_three_part(daily_df["volume"], daily_df["close"])
        from factors.volume import _detect_price_trend
        trend_up = _detect_price_trend(daily_df["close"]) == "up"

    result = assess_fund_for_layer(
        has_northbound=fund_data.get("northbound_available", False),
        has_big_order=fund_data.get("big_order_available", False),
        has_margin=fund_data.get("margin_available", False),
        has_turnover=fund_data.get("turnover_available", False),
        northbound_inflow=fund_data.get("northbound_net_flow", 0),
        amount_trend_up=fund_data.get("turnover_trend_up", False),
        margin_trend=fund_data.get("margin_trend", ""),
        big_order_direction=fund_data.get("big_order_direction", ""),
        for_layer=3, volume_health=vol_health, trend_up=trend_up,
    )

    # D1: 追踪连续净流出天数
    result["consecutive_outflow"] = fund_data.get("consecutive_outflow", False)
    return result


def check_exit_signals(symbol: str, daily_df: pd.DataFrame,
                       weekly_df: pd.DataFrame | None = None,
                       fund_data: dict | None = None,
                       etf_tags: list[str] | None = None,
                       etf_strong_pool: set[str] | None = None,
                       l1_is_strong: bool | None = None) -> dict:
    """
    检查退出信号（Sprint 2 完整版，含缠论卖点）。

    退出信号:
    1. 周线MACD死叉状态 (DIF < DEA)
    2. 日线顶背驰
    3. 缠论卖点（一卖/二卖/三卖）
    4. 资金连续3日净流出
    5. 所属ETF指数跌出强势池
    6. L1 环境转弱

    返回: {triggered: bool, signals: [str], action: str}
    """
    signals = []
    daily_dif, daily_dea, daily_hist = calc_macd(daily_df["close"])

    # 1. 周线MACD死叉状态
    if weekly_df is not None and not weekly_df.empty:
        w_dif, w_dea, _ = calc_macd(weekly_df["close"])
        if bool((w_dif < w_dea).iloc[-1]):
            signals.append("周线MACD死叉")

    # 2. 日线顶背驰
    if check_daily_top_divergence(daily_df, daily_hist):
        signals.append("日线顶背驰")

    # 3. 缠论卖点（Sprint 2 新增）
    sell_point = get_latest_sell_point(daily_df, daily_dif, daily_dea, daily_hist)
    if sell_point["label"] != "无卖点":
        signals.append(f"缠论{sell_point['label']} ({sell_point.get('price', '')})")

    # 4. 资金连续3日净流出
    if fund_data and fund_data.get("consecutive_outflow", False):
        signals.append("资金连续3日净流出")

    # 5. 所属ETF指数跌出强势池 (H9: L2失效减仓)
    if etf_tags and etf_strong_pool is not None:
        fallen = [t for t in etf_tags if t not in etf_strong_pool]
        remaining = [t for t in etf_tags if t in etf_strong_pool]
        if fallen and not remaining:
            signals.append(f"L2 失效: {', '.join(fallen)} 全部跌出强势池，建议减仓")
        elif fallen:
            signals.append(f"所属指数跌出强势池: {', '.join(fallen)}")

    # 6. L1 环境转弱
    if l1_is_strong is not None and not l1_is_strong:
        signals.append("L1 环境转弱")

    if signals:
        return {"triggered": True, "signals": signals, "action": "清仓", "symbol": symbol}

    return {"triggered": False, "signals": [], "action": "持仓", "symbol": symbol}


# ── 跨ETF分散选股（V2.10）──


@dataclass
class SelectionResult:
    """跨ETF分散选股结果"""
    selected: list  # list[StockVerdict]
    by_etf: dict   # dict[str, list[StockVerdict]]
    reserve: list  # list[StockVerdict]
    filled: bool = False
    total_slots: int = 0


def _compute_etf_bonus_coeff(etf_name: str, etf_l2_scores: dict[str, float],
                             enable: bool) -> float:
    """ETF强度加成系数，冷启动暂不启用"""
    if not enable:
        return 1.0
    l2_score = etf_l2_scores.get(etf_name, 50.0)
    raw = 1.0 + (l2_score - 50.0) / 500.0
    return max(0.9, min(1.1, raw))


def select_top_stocks_by_etf(
    candidates: list,
    etf_l2_scores: dict[str, float],
    max_positions: int = 5,
    per_etf_limit: int = 2,
    enable_etf_bonus: bool = True,
) -> SelectionResult:
    """
    按ETF分组排名、跨组取头部，实现分散选股。

    流程:
    1. 按 etf_tags 分组，组内按评分(含可选ETF强度加成)排序
    2. 按组遍历，每组取前 per_etf_limit 只
    3. 去重 + 不超过 max_positions
    4. 未填满则从剩余股票按评分补足
    """
    if not candidates:
        return SelectionResult(selected=[], by_etf={}, reserve=[], total_slots=max_positions)

    # 按ETF分组
    groups: dict[str, list] = {}
    for sv in candidates:
        final_score = sv.score
        if sv.etf_tags:
            best_coeff = max(
                (_compute_etf_bonus_coeff(t, etf_l2_scores, enable_etf_bonus) for t in sv.etf_tags),
                default=1.0,
            )
            final_score = sv.score * best_coeff

        tags = sv.etf_tags if sv.etf_tags else ["__ungrouped__"]
        for tag in tags:
            groups.setdefault(tag, []).append((sv, final_score))

    # 组内排序
    for tag in groups:
        groups[tag] = sorted(groups[tag], key=lambda x: x[1], reverse=True)

    # 组遍历顺序：按组内最高分降序
    group_order = sorted(
        groups.keys(),
        key=lambda g: max(s[1] for s in groups[g]),
        reverse=True,
    )
    if "__ungrouped__" in group_order:
        group_order = [g for g in group_order if g != "__ungrouped__"] + ["__ungrouped__"]

    # 轮询取每组头部
    selected_svs: list = []
    seen: set[str] = set()
    by_etf: dict[str, list] = {}
    ptrs = {tag: 0 for tag in group_order}

    while len(selected_svs) < max_positions:
        added_this_round = False
        for tag in group_order:
            if len(selected_svs) >= max_positions:
                break
            already_picked = len(by_etf.get(tag, []))
            if already_picked >= per_etf_limit:
                ptrs[tag] = len(groups[tag])
                continue
            grp = groups[tag]
            while ptrs[tag] < len(grp):
                sv, _score = grp[ptrs[tag]]
                ptrs[tag] += 1
                if sv.symbol not in seen:
                    seen.add(sv.symbol)
                    selected_svs.append(sv)
                    by_etf.setdefault(tag, []).append(sv)
                    added_this_round = True
                    break
        if not added_this_round:
            break

    # 未填满 → 从剩余股票补足
    reserve = []
    if len(selected_svs) < max_positions:
        for tag in group_order:
            for sv, _score in groups.get(tag, []):
                if sv.symbol not in seen:
                    reserve.append(sv)
                    seen.add(sv.symbol)
                    if len(selected_svs) + len(reserve) >= max_positions:
                        break
            if len(selected_svs) + len(reserve) >= max_positions:
                reserve = reserve[:max_positions - len(selected_svs)]
                break

    return SelectionResult(
        selected=selected_svs,
        by_etf=by_etf,
        reserve=reserve,
        filled=len(selected_svs) >= max_positions,
        total_slots=max_positions,
    )


# ── 上游保鲜验证（V2.10/V2.11）──


@dataclass
class FreshnessResult:
    """上游保鲜验证结果"""
    l1_ok: bool
    l1_note: str = ""
    l2_ok_count: int = 0
    l2_total: int = 0
    l2_failed_etfs: list[str] = field(default_factory=list)
    l3_status: str = "正常运行"  # "正常运行" / "部分冻结" / "全局暂停"
    details: list[str] = field(default_factory=list)


def check_upstream_freshness(
    market_daily: dict[str, pd.DataFrame],        # {shanghai: df, shenzhen: df, chinext: df}
    etf_daily: dict[str, pd.DataFrame] | None = None,  # {ETF名称: df_daily}
) -> FreshnessResult:
    """
    L3 每日运行前验证 L1/L2 日线状态（上游保鲜）。

    L1 保鲜: 三个市场日线 MACD 金叉 + 未跌破 20 日均线
    L2 保鲜: 每个强势 ETF 指数日线 MACD 金叉 + 未跌破 20 日均线

    此验证只影响 L3 是否运行、在哪些 ETF 下运行，
    不修改 L1/L2 的正式评估。
    """
    details = []

    # ── L1 保鲜 ──
    l1_ok = True
    l1_note = "正常"
    failed_markets = []
    checked_count = 0
    for market_name, daily_df in market_daily.items():
        if daily_df.empty or len(daily_df) < 26:
            continue
        checked_count += 1
        result = check_daily_macd_above_ma20(daily_df["close"])

        if not result["all_ok"]:
            failed_markets.append(market_name)
            l1_ok = False

    if checked_count == 0:
        l1_ok = False
        l1_note = "无可用市场数据"

    if not l1_ok:
        l1_note = f"L1 环境已恶化 ({', '.join(failed_markets)})，等待周末周频确认"
        details.append(l1_note)
        return FreshnessResult(
            l1_ok=False, l1_note=l1_note,
            l2_ok_count=0, l2_total=0,
            l3_status="全局暂停", details=details,
        )

    details.append("L1 环境: 正常 ✓")

    # ── L2 保鲜 ──
    l2_ok_count = 0
    l2_total = 0
    l2_failed = []

    if etf_daily:
        l2_total = len(etf_daily)
        for etf_name, daily_df in etf_daily.items():
            if daily_df.empty or len(daily_df) < 26:
                l2_failed.append(f"{etf_name}(数据不足)")
                continue
            from factors.macd import check_daily_macd_above_ma20
            result = check_daily_macd_above_ma20(daily_df["close"])

            if result["all_ok"]:
                l2_ok_count += 1
            else:
                l2_failed.append(etf_name)

    l3_status = "正常运行"
    if etf_daily:
        if l2_ok_count == 0 and l2_total > 0:
            l3_status = "全局暂停"
            details.append("L2 强势指数全部失效")
        elif l2_failed:
            l3_status = "部分冻结"
            details.append(f"L2: {l2_ok_count}/{l2_total} 个 ETF 指数仍强势（已走弱: {', '.join(l2_failed)}）")
        else:
            details.append(f"L2: {l2_ok_count}/{l2_total} 个 ETF 指数正常 ✓")
    else:
        details.append("L2: 无 ETF 数据（跳过保鲜）")

    return FreshnessResult(
        l1_ok=True, l1_note="正常",
        l2_ok_count=l2_ok_count, l2_total=l2_total,
        l2_failed_etfs=l2_failed,
        l3_status=l3_status, details=details,
    )
