"""简化版三层漏斗回测验证 — Sprint 5

验证目标: "三层漏斗能否选出好股票"，不是"精确复制实盘"。

流程:
  1. 预计算：对所有候选股票跑一次 L3 评估（每日线只算一次），按月缓存评分
  2. 周循环：每周五 L1(市场→快) → L2(ETF→快) → 查当月 L3 缓存选股

用法:
  python -m backtest.runner                          # 全量
  python -m backtest.runner --quick                   # 快速（只测2023-2024）
  python -m backtest.runner --quick --n-stocks 300    # 指定股票数量
"""

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

from data.store import DataStore
from core.logger import get_logger

logger = get_logger("backtest.runner")


@dataclass
class YearlyLayerStats:
    """单年度分层统计"""
    def __init__(self, year: int = 0):
        self.year = year
        self.l1_checks = 0
        self.l1_passes = 0
        self.l1_states = {"牛市": 0, "震荡": 0, "偏弱": 0, "熊市": 0}
        self.l2_checks = 0
        self.l2_passes = 0
        self.strong_sectors_avg = 0.0
        self.strong_sectors_sum = 0
        self.l3_avg_picks = 0.0
        self.l3_picks_sum = 0
        self.l3_trade_count = 0
        self.l3_top_score_avg = 0.0
        self.l3_top_score_sum = 0.0
        self.annual_return = 0.0
        self.max_drawdown = 0.0
        self.sharpe = 0.0
        self.excess_return = 0.0


@dataclass
class BacktestResult:
    """回测结果"""
    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    empty_position_ratio: float = 0.0
    layer1_pass_rate: float = 0.0
    layer2_pass_rate: float = 0.0
    layer3_avg_picks: float = 0.0
    trades: list[dict] = field(default_factory=list)
    yearly: dict[int, YearlyLayerStats] = field(default_factory=dict)


def calc_metrics(daily_returns: pd.Series, benchmark_returns: pd.Series | None = None,
                 rf: float = 0.02) -> dict:
    er = daily_returns.dropna()
    if er.empty:
        return {}
    total_days = len(er)
    years = total_days / 252
    total_ret = (1 + er).prod() - 1
    annual_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0
    annual_vol = er.std() * np.sqrt(252)
    sharpe = (annual_ret - rf) / annual_vol if annual_vol > 0 else 0
    cummax = (1 + er).cumprod().cummax()
    drawdown = (1 + er).cumprod() / cummax - 1
    max_dd = drawdown.min()
    win_rate = (er > 0).mean()
    bm_total = None
    if benchmark_returns is not None and not benchmark_returns.empty:
        br = benchmark_returns.reindex(er.index).dropna()
        if len(br) > 0:
            bm_total = float((1 + br).prod() - 1)
    return {
        "total_return": total_ret, "annual_return": annual_ret,
        "max_drawdown": max_dd, "win_rate": win_rate, "sharpe_ratio": sharpe,
        "benchmark_return": bm_total or 0.0, "excess_return": total_ret - (bm_total or 0.0),
    }


# ── L1: 大盘环境评估 ──


def evaluate_l1(market_weekly: dict, market_monthly: dict,
                date_str: str) -> tuple[bool, float, str]:
    """评估第一层。返回 (passed, position_pct, market_state)"""
    from gate.layer1_market import assess_market

    try:
        w_data = {k: v.loc[:date_str] for k, v in market_weekly.items() if not v.empty}
        m_data = {k: v.loc[:date_str] for k, v in market_monthly.items() if not v.empty}
        l1 = assess_market(w_data, monthly_data=m_data)
        return l1.passed, l1.actual_position_pct, l1.market_state
    except Exception as e:
        logger.error(f"L1 {date_str}: {e}")
        return False, 0.0, "熊市"


# ── L2: ETF分类指数评估 ──


def evaluate_l2(etf_weekly: dict, benchmark: pd.Series | None,
                etf_daily: dict | None = None, date_str: str = "",
                l1_market_state: str = "volatile",
                etf_monthly: dict | None = None) -> tuple[bool, list[str]]:
    """评估第二层。返回 (passed, strong_sectors)"""
    from gate.layer2_sector import assess_sectors

    # L1中文状态 → L2英文状态
    _state_map = {"牛市": "bull", "震荡": "volatile", "偏弱": "weak", "熊市": "bear"}
    l1_state_en = _state_map.get(l1_market_state, "volatile")

    try:
        etf_data = {k: v.loc[:date_str] for k, v in etf_weekly.items() if not v.empty}
        bm = benchmark.loc[:date_str] if benchmark is not None else None
        daily_data = {k: v.loc[:date_str] for k, v in (etf_daily or {}).items() if not v.empty}
        monthly_data = {k: v.loc[:date_str] for k, v in (etf_monthly or {}).items() if not v.empty}
        l2 = assess_sectors(etf_data, benchmark_close=bm, daily_data=daily_data if daily_data else None,
                           l1_market_state=l1_state_en, monthly_data=monthly_data if monthly_data else None)
        if l2.passed:
            return True, [s.etf_name for s in l2.strong_sectors]
        return False, []
    except Exception as e:
        logger.error(f"L2 {date_str}: {e}")
        return False, []


# ── L3: 个股评分预计算（批量截面处理）──

# 回测用 ETF 列表（与 data_loader 一致）
_ETF_NAMES = ["证券", "银行", "军工", "芯片", "半导体", "新能源车", "光伏", "消费", "医药", "酒", "科技", "有色", "煤炭", "汽车"]
_MARKET_NAMES = ["shanghai", "shenzhen", "chinext"]


def _build_concept_indices(
    store: DataStore,
    symbols: list[str],
    symbol_etf_map: dict[str, list[str]],
    n_stocks_per_concept: int = 15,
) -> dict[str, pd.DataFrame]:
    """
    从股票数据构建合成概念板块指数。

    使用 sector_mapper.CONCEPT_TO_ETF 映射，为每个概念板块:
    1. 找到映射的目标ETF
    2. 从 symbol_etf_map 中取与该ETF相关的股票
    3. 取相关性最高的 n_stocks_per_concept 只
    4. 等权平均日收益率构建合成指数

    返回: {concept_name: DataFrame(close/open/high/low/volume)}
    """
    from gate.sector_mapper import CONCEPT_TO_ETF

    concept_indices = {}
    all_dates = set()

    # 收集所有股票日期的并集
    for sym in symbols:
        df = store.get_daily(sym)
        if df is not None:
            all_dates.update(df.index)
    all_dates = sorted(all_dates)

    for concept_name, target_etfs in CONCEPT_TO_ETF.items():
        # 找到映射到目标ETF的股票
        matched_stocks = []
        for sym in symbols:
            tags = symbol_etf_map.get(sym, [])
            if any(etf in tags for etf in target_etfs):
                matched_stocks.append(sym)
                if len(matched_stocks) >= n_stocks_per_concept:
                    break

        if len(matched_stocks) < 3:
            continue

        # 构建等权合成指数
        rets_list = []
        for sym in matched_stocks:
            df = store.get_daily(sym)
            if df is None:
                continue
            ret = df["close"].pct_change()
            rets_list.append(ret)

        if len(rets_list) < 3:
            continue

        # 对齐收益率并取均值
        ret_df = pd.concat(rets_list, axis=1)
        avg_ret = ret_df.mean(axis=1).dropna()
        if len(avg_ret) < 26:
            continue

        # 反推价格序列
        close = (1 + avg_ret).cumprod() * 100
        fake_vol = pd.Series(1e6, index=close.index)
        high = close * 1.02
        low = close * 0.98
        open_p = close.shift(1)

        concept_df = pd.DataFrame({
            "close": close, "open": open_p,
            "high": high, "low": low,
            "volume": fake_vol,
        }).dropna()

        if len(concept_df) >= 26:
            concept_indices[concept_name] = concept_df
            logger.debug(f"概念板块: {concept_name} ({len(matched_stocks)}只成分股, "
                        f"{len(concept_df)}天数据)")

    logger.info(f"构建概念板块指数: {len(concept_indices)} 个")
    return concept_indices


def _build_etf_tags_map(store: DataStore, symbols: list[str]) -> dict[str, list[str]]:
    """构建 symbol → etf_tags 映射。

    优先从缓存加载 etf_tags.parquet，其次用价格相关性兜底。
    """
    import json

    cache_path = Path("data/raw/_cache/backtest/etf_tags.parquet")

    # 优先读缓存
    if cache_path.exists():
        try:
            df_cache = pd.read_parquet(cache_path)
            # 预期列: symbol, etf_tags(list或json)
            result = {}
            for _, row in df_cache.iterrows():
                sym = row.get("symbol", "")
                tags = row.get("etf_tags", [])
                if isinstance(tags, str):
                    tags = json.loads(tags)
                if hasattr(tags, "tolist"):
                    tags = tags.tolist()
                result[sym] = tags if isinstance(tags, list) else []
            logger.info(f"从缓存加载 etf_tags: {len(result)} 条")
            return result
        except Exception as e:
            logger.warning(f"etf_tags 缓存加载失败: {e}")

    # 价格相关性兜底
    logger.info("计算价格相关性映射...")
    result = {}
    etf_daily = {}
    for etf_name in _ETF_NAMES:
        df = store.get_daily(etf_name)
        if df is not None and len(df) >= 20:
            etf_daily[etf_name] = df

    if not etf_daily:
        logger.warning("无ETF日线数据，etf_tags为空")
        return {s: [] for s in symbols}

    for sym in symbols:
        stock_df = store.get_daily(sym)
        if stock_df is None or len(stock_df) < 20:
            result[sym] = []
            continue

        stock_ret = stock_df["close"].pct_change().dropna()
        corrs = {}
        for etf_name, etf_df in etf_daily.items():
            etf_ret = etf_df["close"].pct_change().dropna()
            aligned = pd.concat([stock_ret, etf_ret], axis=1).dropna()
            if len(aligned) < 20:
                continue
            corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
            if corr > 0.4:
                corrs[etf_name] = corr

        if corrs:
            top = sorted(corrs, key=corrs.get, reverse=True)[:3]
            result[sym] = top
        else:
            result[sym] = []

    # 保存缓存
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"symbol": s, "etf_tags": tags} for s, tags in result.items()]
        pd.DataFrame(rows).to_parquet(cache_path, index=False)
        logger.info(f"etf_tags 缓存已保存: {len(result)} 条")
    except Exception as e:
        logger.warning(f"etf_tags 缓存保存失败: {e}")

    return result


def precompute_all_stocks(
    store: DataStore,
    hs300_daily: pd.DataFrame | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """预计算所有股票的每月 L3 评分（批量截面处理）。

    流程:
    1. 收集所有月末时点
    2. 逐月：批量计算原始因子 → 截面标准化 → 加权评分
    3. 逐只过门（Gate 3取2）
    4. 返回 DataFrame: symbol, month, passed, score, classification, etf_tags
    """
    from gate.layer3_stock import assess_stock
    from factors.multi_factor import (
        compute_raw_factors, process_cross_section, aggregate_scores,
        apply_hard_filters, check_market_trend,
    )

    symbols = store.stock_names
    logger.info(f"预计算 L3 评分: {len(symbols)} 只股票（批量截面）...")

    # ── 构建 symbol → etf_tags 映射（一次性）──
    symbol_etf_map = _build_etf_tags_map(store, symbols)
    n_mapped = sum(1 for v in symbol_etf_map.values() if v)
    logger.info(f"ETF标签映射: {n_mapped}/{len(symbols)} 只股票有归属")

    # 收集所有月末时点
    all_month_ends: set[pd.Timestamp] = set()
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is not None and len(daily) >= 200:
            ends = daily.resample("ME").groups.keys()
            all_month_ends.update(ends)
    all_month_ends = sorted(all_month_ends)

    # 过滤到回测期间（需要前推6个月让 L3 缓存提前就绪）
    if start is not None:
        pre_start = pd.Timestamp(start) - pd.DateOffset(months=6)
        all_month_ends = [m for m in all_month_ends if m >= pre_start]
    if end is not None:
        end_ts = pd.Timestamp(end)
        all_month_ends = [m for m in all_month_ends if m <= end_ts]

    if not all_month_ends:
        logger.warning("无符合条件的月末时点")
        return pd.DataFrame()

    logger.info(f"月末时点: {len(all_month_ends)} 个 ({all_month_ends[0].date()} ~ {all_month_ends[-1].date()})")

    all_rows = []

    for month_end in tqdm(all_month_ends, desc="L3月截面"):
        month_str = month_end.strftime("%Y-%m")

        # 大盘过滤（检查点4: t-1时点）
        alpha_mult = 1.0
        if hs300_daily is not None:
            alpha_mult = check_market_trend(hs300_daily, month_end)

        # Step 1: 批量计算原始因子
        all_factors = []
        for sym in symbols:
            daily = store.get_daily(sym)
            if daily is None or len(daily) < 200:
                continue
            daily_cut = daily.loc[:month_end]
            if len(daily_cut) < 50:
                continue

            weekly = store.get_weekly(sym)
            monthly = store.get_monthly(sym)
            weekly_cut = weekly.loc[:month_end] if weekly is not None else None
            monthly_cut = monthly.loc[:month_end] if monthly is not None else None

            # 检查点1: 月线数据严格不越界
            if monthly_cut is not None and len(monthly_cut) > 0:
                assert monthly_cut.index[-1] <= month_end, \
                    f"{sym}: 月线越界 {monthly_cut.index[-1]} > {month_end}"

            f = compute_raw_factors(sym, daily_cut, weekly_cut, monthly_cut)
            if f is not None:
                f["_daily_cut"] = daily_cut
                f["_weekly_cut"] = weekly_cut
                f["_monthly_cut"] = monthly_cut
                all_factors.append(f)

        if not all_factors:
            continue

        # Step 2: 硬约束 + 截面标准化
        factors_df = pd.DataFrame(all_factors)
        factors_df = apply_hard_filters(factors_df)
        if factors_df.empty:
            continue

        # 分离元数据
        meta_cols = ["_daily_cut", "_weekly_cut", "_monthly_cut"]
        meta = {c: factors_df[c].tolist() if c in factors_df.columns else [] for c in meta_cols}
        factor_cols = [c for c in factors_df.columns if c not in meta_cols and c != "symbol"]
        factor_data = factors_df[["symbol"] + factor_cols].copy()

        processed = process_cross_section(factor_data.to_dict("records"))
        if processed.empty:
            continue

        # Step 3: 评分合成
        scores = aggregate_scores(processed, alpha_multiplier=alpha_mult)

        # Step 4: 逐只过门
        for idx, score_row in scores.iterrows():
            sym = idx
            # 找回原始元数据
            daily_cut = None
            weekly_cut = None
            monthly_cut = None
            for i, s in enumerate(factors_df["symbol"]):
                if s == sym:
                    daily_cut = meta["_daily_cut"][i] if i < len(meta["_daily_cut"]) else None
                    weekly_cut = meta["_weekly_cut"][i] if i < len(meta["_weekly_cut"]) else None
                    monthly_cut = meta["_monthly_cut"][i] if i < len(meta["_monthly_cut"]) else None
                    break

            if daily_cut is None:
                continue

            factor_dict = {
                "score": float(score_row["score"]),
                "trend_score": float(score_row.get("trend_score", 0)),
                "alpha_score": float(score_row.get("alpha_score", 0)),
                "risk_score": float(score_row.get("risk_score", 0)),
            }

            try:
                v = assess_stock(
                    sym, daily_df=daily_cut,
                    weekly_df=weekly_cut,
                    monthly_df=monthly_cut,
                    factor_scores=factor_dict,
                )
                all_rows.append({
                    "symbol": sym, "month": month_str,
                    "passed": v.passed_gate, "score": v.score,
                    "classification": v.classification,
                    "etf_tags": symbol_etf_map.get(sym, []),
                })
            except Exception:
                continue

    result = pd.DataFrame(all_rows)
    logger.info(f"L3预计算完成: {len(result)} 条记录, "
                f"通过率 {result['passed'].mean():.1%}" if len(result) > 0 else "L3预计算完成: 0条")
    return result


def _precompute_stock_monthly(sym: str, store: DataStore) -> list[dict]:
    """向后兼容的逐只预计算（无截面标准化，使用退化评分）。

    用于 scripts/update_data.py 等少量股票增量更新的场景。
    全量回测请使用 precompute_all_stocks()。
    """
    from gate.layer3_stock import assess_stock

    daily_df = store.get_daily(sym) if hasattr(store, 'get_daily') else None
    if daily_df is None:
        return []

    if not isinstance(daily_df, pd.DataFrame):
        daily_df = store  # 兼容直接传 DataFrame 的旧调用

    if isinstance(daily_df, pd.DataFrame):
        if len(daily_df) < 200:
            return []
        results = []
        monthly_groups = daily_df.resample("ME")
        for month_end, month_df in monthly_groups:
            if len(month_df) < 10:
                continue
            month_str = str(month_end.date())
            try:
                hist_start = month_end - pd.DateOffset(days=365)
                hist = daily_df.loc[hist_start:month_end]
                if len(hist) < 50:
                    continue
                v = assess_stock(sym, daily_df=hist)
                results.append({
                    "symbol": sym, "month": month_str[:7],
                    "passed": v.passed_gate, "score": v.score,
                    "classification": v.classification,
                    "etf_tags": v.etf_tags,
                })
            except Exception:
                continue
        return results
    return []


# ── 主回测循环 ──


def run_funnel_backtest(
    store: DataStore,
    l3_scores: pd.DataFrame | None = None,
    start: str = "2020-01-01",
    end: str = "2024-12-31",
    top_n: int = 10,
    concept_indices: dict[str, pd.DataFrame] | None = None,
) -> BacktestResult:
    """三层漏斗回测。

    每周五:
    1. L1: 评估4个市场指数 → 通过才继续
    2. L2: 评估ETF分类指数 + 概念板块 → 通过才继续
    3. L3: 查当月预计算评分，只选L2强势板块内股票，选 top_n 只
    """
    # 从 DataStore 构建所需 dict（使用统一 resample 规则）
    market_weekly = {n: store.get_weekly(n) for n in _MARKET_NAMES if store.get_weekly(n) is not None}
    market_monthly = {n: store.get_monthly(n) for n in _MARKET_NAMES if store.get_monthly(n) is not None}
    etf_weekly = {n: store.get_weekly(n) for n in _ETF_NAMES if store.get_weekly(n) is not None}
    etf_daily = {n: store.get_daily(n) for n in _ETF_NAMES if store.get_daily(n) is not None}
    etf_monthly = {n: store.get_monthly(n) for n in _ETF_NAMES if store.get_monthly(n) is not None}

    # ── 概念板块合并到 L2 ──
    if concept_indices:
        n_concepts = 0
        for cname, cdf in concept_indices.items():
            if cname in etf_weekly:
                continue
            if len(cdf) < 26:
                continue
            # pandas 周线 resample (周五)
            c_weekly = cdf.resample("W-FRI").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna()
            if len(c_weekly) < 4:
                continue
            etf_weekly[cname] = c_weekly
            etf_daily[cname] = cdf
            n_concepts += 1
        logger.info(f"L2 合并: {len(_ETF_NAMES)} ETF + {n_concepts} 概念板块")

    # 基准: 沪深300
    benchmark = None
    bm_daily = store.get_daily("csi300")
    if bm_daily is not None:
        benchmark = bm_daily["close"]

    result = BacktestResult()

    weekly_dates = sorted(set().union(
        *[df.index for df in market_weekly.values() if not df.empty]
    ))
    weekly_dates = [d for d in weekly_dates if start <= str(d)[:10] <= end]
    if len(weekly_dates) < 12:
        logger.warning(f"数据不足12周 ({len(weekly_dates)})")
        return result

    layer1_checks = 0
    layer1_passes = 0
    layer2_checks = 0
    layer2_passes = 0
    empty_days = 0
    trades = []

    # 按年跟踪
    yearly_data: dict[int, dict] = {}  # year → raw data for later stat calc

    curve = [1.0]
    bm_curve = [1.0]
    holdings = []
    prev_week = pd.Timestamp(start)

    for w_idx, w_date in enumerate(tqdm(weekly_dates, desc="周回测")):
        w_str = str(w_date)[:10]
        wy = pd.Timestamp(w_str).year

        # 初始化年度数据
        if wy not in yearly_data:
            yearly_data[wy] = {
                "l1_checks": 0, "l1_passes": 0, "l1_states": {"牛市": 0, "震荡": 0, "偏弱": 0, "熊市": 0},
                "l2_checks": 0, "l2_passes": 0, "strong_sectors_sum": 0,
                "l3_picks_sum": 0, "l3_trades": 0, "l3_top_score_sum": 0.0,
                "curve": [1.0], "bm_curve": [1.0],
            }

        # ── L1: 大盘（动态仓位）──
        layer1_checks += 1
        l1_ok, pos_pct, l1_state = evaluate_l1(market_weekly, market_monthly, w_str)

        yd = yearly_data[wy]
        yd["l1_checks"] += 1
        if pos_pct > 0:
            layer1_passes += 1
            yd["l1_passes"] += 1
        if l1_state in yd["l1_states"]:
            yd["l1_states"][l1_state] += 1

        if pos_pct == 0:
            holdings = []
            _fill_flat(curve, bm_curve, prev_week, w_date, benchmark)
            empty_days += max(0, len(pd.bdate_range(prev_week, w_date)) - 1)
            prev_week = w_date
            continue

        # ── L2: ETF指数 ──
        layer2_checks += 1
        l2_ok, strong_sectors = evaluate_l2(etf_weekly, benchmark, etf_daily, w_str,
                                             l1_market_state=l1_state, etf_monthly=etf_monthly)

        yd = yearly_data[wy]
        yd["l2_checks"] += 1
        if l2_ok:
            layer2_passes += 1
            yd["l2_passes"] += 1
        yd["strong_sectors_sum"] += len(strong_sectors) if strong_sectors else 0

        if not l2_ok or not strong_sectors:
            holdings = []
            _fill_flat(curve, bm_curve, prev_week, w_date, benchmark)
            empty_days += max(0, len(pd.bdate_range(prev_week, w_date)) - 1)
            prev_week = w_date
            continue

        # ── L3: 查上月评分，按L2强势板块过滤 ──
        month_key = (pd.Timestamp(w_str) - pd.DateOffset(months=1)).strftime("%Y-%m")
        month_scores = l3_scores[
            (l3_scores["month"] == month_key) & (l3_scores["passed"])
        ] if l3_scores is not None else pd.DataFrame()

        if month_scores.empty:
            if not holdings:
                _fill_flat(curve, bm_curve, prev_week, w_date, benchmark)
                empty_days += max(0, len(pd.bdate_range(prev_week, w_date)) - 1)
                prev_week = w_date
                continue

        # L2强势板块过滤: etf_tags 与 strong_sectors 有交集
        if strong_sectors and "etf_tags" in month_scores.columns:
            def _in_strong(tags):
                if not isinstance(tags, list) or not tags:
                    return False
                return bool(set(tags) & set(strong_sectors))
            in_pool = month_scores["etf_tags"].apply(_in_strong)
            pool_scores = month_scores[in_pool]
            # 保底: 若强势池内无股票，回退到全量（至少选得出）
            if len(pool_scores) >= 3:
                month_scores = pool_scores

        top_stocks = month_scores.nlargest(top_n, "score")
        holdings = top_stocks["symbol"].tolist()

        trades.append({
            "date": w_str, "n_picks": len(holdings),
            "l1_pct": pos_pct, "strong_sectors": strong_sectors,
            "top_score": float(top_stocks["score"].max()) if not top_stocks.empty else 0,
        })
        yd["l3_trades"] += 1
        yd["l3_picks_sum"] += len(holdings)
        yd["l3_top_score_sum"] += float(top_stocks["score"].max()) if not top_stocks.empty else 0

        # 填充该周的日收益率: 从决策日到下一决策日（避免前视偏差）
        next_w_date = weekly_dates[w_idx + 1] if w_idx + 1 < len(weekly_dates) else w_date + pd.DateOffset(days=7)
        week_days = pd.bdate_range(w_date, next_w_date)
        if len(week_days) > 0:
            week_days = week_days[1:]  # 跳过决策日本身
        for d in week_days:
            ds = str(d)[:10]
            day_ret = 0.0
            bm_day_ret = 0.0

            if holdings:
                rets = []
                for sym in holdings:
                    sdf = store.get_daily(sym)
                    if sdf is not None and ds in sdf.index:
                        idx = sdf.index.get_loc(ds)
                        if idx > 0:
                            prev_c = sdf["close"].iloc[idx - 1]
                            cur_c = sdf["close"].iloc[idx]
                            if prev_c > 0:
                                rets.append(cur_c / prev_c - 1)
                if rets:
                    day_ret = np.mean(rets) * pos_pct
                    curve.append(curve[-1] * (1 + day_ret))
                else:
                    curve.append(curve[-1])
                    empty_days += 1
            else:
                curve.append(curve[-1])
                empty_days += 1

            # 基准
            if benchmark is not None and ds in benchmark.index:
                bm_idx = benchmark.index.get_loc(ds)
                if bm_idx > 0:
                    bm_day_ret = float(benchmark.iloc[bm_idx] / benchmark.iloc[bm_idx - 1] - 1)
                    bm_curve.append(bm_curve[-1] * (1 + bm_day_ret))
                else:
                    bm_curve.append(bm_curve[-1])
            else:
                bm_curve.append(bm_curve[-1])

            # 按年曲线跟踪
            yd["curve"].append(yd["curve"][-1] * (1 + day_ret))
            yd["bm_curve"].append(yd["bm_curve"][-1] * (1 + bm_day_ret))

        prev_week = w_date

    # 指标
    pc = pd.Series(curve).pct_change().dropna()
    total_trading_days = len(curve) - 1
    empty_pct = empty_days / max(total_trading_days, 1)
    metrics = calc_metrics(pc, benchmark_returns=pd.Series(bm_curve).pct_change().dropna())

    result.trades = trades
    result.total_return = metrics.get("total_return", 0)
    result.annual_return = metrics.get("annual_return", 0)
    result.max_drawdown = metrics.get("max_drawdown", 0)
    result.win_rate = metrics.get("win_rate", 0)
    result.sharpe_ratio = metrics.get("sharpe_ratio", 0)
    result.benchmark_return = metrics.get("benchmark_return", 0)
    result.excess_return = metrics.get("excess_return", 0)
    result.empty_position_ratio = empty_pct
    result.layer1_pass_rate = layer1_passes / max(layer1_checks, 1)
    result.layer2_pass_rate = layer2_passes / max(layer2_checks, 1)
    result.layer3_avg_picks = np.mean([t["n_picks"] for t in trades]) if trades else 0

    # ── 按年汇总 ──
    for wy, yd in sorted(yearly_data.items()):
        ys = YearlyLayerStats(wy)
        # L1
        ys.l1_checks = yd["l1_checks"]
        ys.l1_passes = yd["l1_passes"]
        ys.l1_states = yd["l1_states"]
        # L2
        ys.l2_checks = yd["l2_checks"]
        ys.l2_passes = yd["l2_passes"]
        ys.strong_sectors_sum = yd["strong_sectors_sum"]
        ys.strong_sectors_avg = yd["strong_sectors_sum"] / max(yd["l2_checks"], 1)
        # L3
        ys.l3_trade_count = yd["l3_trades"]
        ys.l3_picks_sum = yd["l3_picks_sum"]
        ys.l3_avg_picks = yd["l3_picks_sum"] / max(yd["l3_trades"], 1)
        ys.l3_top_score_sum = yd["l3_top_score_sum"]
        ys.l3_top_score_avg = yd["l3_top_score_sum"] / max(yd["l3_trades"], 1)
        # 收益
        yc = pd.Series(yd["curve"]).pct_change().dropna()
        if len(yc) > 0:
            y_metrics = calc_metrics(yc)
            ys.annual_return = y_metrics.get("annual_return", 0)
            ys.max_drawdown = y_metrics.get("max_drawdown", 0)
            ys.sharpe = y_metrics.get("sharpe_ratio", 0)
        ybm = pd.Series(yd["bm_curve"]).pct_change().dropna()
        if len(yc) > 0 and len(ybm) > 0:
            yr_total = (1 + yc).prod() - 1
            yr_bm = (1 + ybm).prod() - 1
            ys.excess_return = yr_total - yr_bm
        result.yearly[wy] = ys

    return result


def _fill_flat(curve, bm_curve, prev_date, cur_date, benchmark):
    """填充空仓期的曲线（无收益）。跳过边界日避免与主循环重复计算。"""
    days = pd.bdate_range(prev_date, cur_date)
    if len(days) > 0:
        days = days[1:]
    for d in days:
        curve.append(curve[-1])
        ds = str(d)[:10]
        if benchmark is not None and ds in benchmark.index:
            bm_idx = benchmark.index.get_loc(ds)
            if bm_idx > 0:
                bm_ret = float(benchmark.iloc[bm_idx] / benchmark.iloc[bm_idx - 1] - 1)
                bm_curve.append(bm_curve[-1] * (1 + bm_ret))
            else:
                bm_curve.append(bm_curve[-1])
        else:
            bm_curve.append(bm_curve[-1])


def print_report(r: BacktestResult):
    print("=" * 78)
    print("  三层漏斗回测验证报告")
    print("=" * 78)
    print(f"  累计收益:     {r.total_return:>+.2%}")
    print(f"  年化收益:     {r.annual_return:>+.2%}")
    print(f"  最大回撤:     {r.max_drawdown:>-.2%}")
    print(f"  夏普比率:     {r.sharpe_ratio:>.3f}")
    print(f"  胜率:         {r.win_rate:>.1%}")
    print(f"  基准收益:     {r.benchmark_return:>+.2%}")
    print(f"  超额收益:     {r.excess_return:>+.2%}")
    print(f"  ──────────────────────────────")
    print(f"  空仓占比:     {r.empty_position_ratio:>.1%}")
    print(f"  L1通过率:     {r.layer1_pass_rate:>.1%}")
    print(f"  L2通过率:     {r.layer2_pass_rate:>.1%}")
    print(f"  L3平均选股:   {r.layer3_avg_picks:.1f} 只")
    print(f"  调仓次数:     {len(r.trades)}")

    # ── 按年分层展示 ──
    if r.yearly:
        print()
        print("  ╔" + "═" * 74 + "╗")
        print("  ║" + "  按年分层指标".center(68) + "║")
        print("  ╠" + "═" * 74 + "╣")

        # 表头
        header = (f"  ║ {'年份':^6} │ {'L1通过':^8} │ {'L1主要状态':^16} │ "
                  f"{'L2通过':^8} │ {'L2强势板块':^10} │ "
                  f"{'L3均选':^6} │ {'年收益':^8} │ {'超额':^8} ║")
        print(header)
        print("  ╟" + "─" * 74 + "╢")

        for wy in sorted(r.yearly.keys()):
            ys = r.yearly[wy]
            # L1主要状态
            dominant = max(ys.l1_states, key=ys.l1_states.get) if ys.l1_states else "—"
            l1_rate = ys.l1_passes / max(ys.l1_checks, 1)
            l2_rate = ys.l2_passes / max(ys.l2_checks, 1)
            line = (f"  ║ {wy:^6} │ {l1_rate:>7.1%} │ {dominant:^16} │ "
                    f"{l2_rate:>7.1%} │ {ys.strong_sectors_avg:>8.1f}  │ "
                    f"{ys.l3_avg_picks:>5.1f} │ {ys.annual_return:>+7.2%} │ {ys.excess_return:>+7.2%} ║")
            print(line)

        print("  ╚" + "═" * 74 + "╝")

    print("=" * 78)


def main():
    parser = argparse.ArgumentParser(description="三层漏斗回测验证")
    parser.add_argument("--quick", action="store_true", help="快速模式 (只测2023-2024)")
    parser.add_argument("--start", type=str, default=None, help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--n-stocks", type=int, default=200, help="个股数量")
    parser.add_argument("--no-concepts", action="store_true", help="禁用概念板块（仅用ETF）")
    args = parser.parse_args()

    cache_dir = Path("data/raw/_cache/backtest")

    from backtest.data_loader import load_all_from_cache

    store = load_all_from_cache(cache_dir, n_stocks=args.n_stocks)
    n_stocks = len(store.stock_names)
    n_indices = len(store.index_names)
    logger.info(f"加载完成: {n_indices} 指数, {n_stocks} 个股")

    if args.start and args.end:
        start, end = args.start, args.end
    elif args.quick:
        start, end = "2023-01-01", "2024-12-31"
    else:
        start, end = "2019-01-01", "2026-04-30"

    # ── 构建 ETF tag 映射 ──
    symbol_etf_map = _build_etf_tags_map(store, store.stock_names)
    n_mapped = sum(1 for v in symbol_etf_map.values() if v)
    logger.info(f"ETF映射: {n_mapped}/{n_stocks} 只")

    # ── L3 预计算（只跑一次，带缓存）──
    l3_cache_dir = cache_dir / "precompute"
    l3_cache_path = l3_cache_dir / f"l3_scores_{start}_{end}_{args.n_stocks}.parquet"
    l3_scores = None
    if l3_cache_path.exists():
        l3_scores = pd.read_parquet(l3_cache_path)
        logger.info(f"L3 从缓存加载: {len(l3_scores)} 条")
    if l3_scores is None or len(l3_scores) == 0:
        l3_scores = precompute_all_stocks(store, start=start, end=end)
        l3_cache_dir.mkdir(parents=True, exist_ok=True)
        l3_scores.to_parquet(l3_cache_path, index=False)
        logger.info(f"L3 缓存已保存: {l3_cache_path}")

    # ── 概念板块指数 ──
    concept_indices = None
    if not args.no_concepts:
        concept_indices = _build_concept_indices(store, store.stock_names, symbol_etf_map)

    # ── 主回测 ──
    result = run_funnel_backtest(
        store=store,
        l3_scores=l3_scores,
        start=start, end=end,
        top_n=10,
        concept_indices=concept_indices,
    )

    print_report(result)
    return result


if __name__ == "__main__":
    main()
