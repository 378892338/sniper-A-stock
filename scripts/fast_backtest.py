"""快速回测 — 滚动指标预计算 + 按月查表

提速原理:
  旧: 42月 × 500只 × compute_raw_factors() (滚动计算重复42次)
  新: 500只 × precompute_stock_all_factors() (一次性算完) + 按月查表

用法:
  python -m scripts.fast_backtest --n-stocks 500 --start 2023-01-01 --end 2025-12-31
  python -m scripts.fast_backtest --quick --n-stocks 200
"""

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.store import DataStore
from core.logger import get_logger

logger = get_logger("fast_backtest")


def _precompute_one(sym: str, cache_path: Path) -> tuple[str, pd.DataFrame | None]:
    """为单只股票预计算全时间序列因子（在子进程中运行）"""
    try:
        from data.store import DataStore
        store = DataStore.from_parquet_cache(cache_path)
        daily = store.get_daily(sym)
        if daily is None or len(daily) < 252:
            return sym, None
        weekly = store.get_weekly(sym)
        monthly = store.get_monthly(sym)

        from factors.precompute import precompute_stock_all_factors
        result = precompute_stock_all_factors(daily, weekly, monthly)
        return sym, result
    except Exception as e:
        return sym, None


def fast_precompute_all(
    symbols: list[str],
    cache_path: Path,
    n_workers: int = 4,
) -> dict[str, pd.DataFrame]:
    """并行预计算所有股票的因子时间序列。

    返回: {symbol: precomputed_df}
    """
    logger.info(f"预计算 {len(symbols)} 只股票的滚动因子（{n_workers} 进程并行）...")
    t0 = time.time()

    results: dict[str, pd.DataFrame] = {}
    done = 0

    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(_precompute_one, sym, cache_path): sym
            for sym in symbols
        }
        with tqdm(total=len(symbols), desc="预计算") as pbar:
            for f in as_completed(futures):
                sym, df = f.result()
                if df is not None:
                    results[sym] = df
                done += 1
                pbar.update(1)
                pbar.set_postfix(ok=len(results), fail=done - len(results))

    elapsed = time.time() - t0
    logger.info(
        f"预计算完成: {len(results)}/{len(symbols)} 成功, "
        f"耗时 {elapsed:.0f}s ({elapsed / 60:.1f}min)"
    )
    return results


def fast_monthly_loop(
    store: DataStore,
    precomputed: dict[str, pd.DataFrame],
    all_month_ends: list[pd.Timestamp],
    symbols: list[str],
    hs300_daily: pd.DataFrame | None = None,
    etf_tags_map: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """按月查表 + 截面标准化 + 评分 + 过门。

    返回: l3_scores DataFrame (与 runner.py 格式一致)
    """
    from factors.multi_factor import (
        apply_hard_filters, process_cross_section, aggregate_scores,
        check_market_trend,
    )
    from gate.layer3_stock import assess_stock

    if etf_tags_map is None:
        etf_tags_map = {}

    # ── 预缓存日周月数据（免去每月重复读取）──
    logger.info("缓存日周月数据（一次性加载全历史）...")
    daily_all: dict[str, pd.DataFrame] = {}
    weekly_all: dict[str, pd.DataFrame | None] = {}
    monthly_all: dict[str, pd.DataFrame | None] = {}
    for sym in tqdm(symbols, desc="数据缓存"):
        daily_all[sym] = store.get_daily(sym)
        weekly_all[sym] = store.get_weekly(sym)
        monthly_all[sym] = store.get_monthly(sym)
    logger.info(f"数据缓存完成: {sum(1 for v in daily_all.values() if v is not None)}/{len(symbols)}")

    # ── 预计算形态检测序列（一次性全历史，免去每月重复计算）──
    from factors.precompute import precompute_stock_patterns
    pattern_cache: dict[str, dict] = {}
    logger.info("预计算形态检测序列（一次性全历史）...")
    for sym in tqdm(symbols, desc="形态预计算"):
        daily = daily_all.get(sym)
        if daily is not None and len(daily) >= 200:
            try:
                pattern_cache[sym] = precompute_stock_patterns(daily)
            except Exception as e:
                logger.warning(f"形态预计算失败 [{sym}]: {e}")
    logger.info(f"形态预计算完成: {len(pattern_cache)}/{len(symbols)}")

    all_rows = []

    for month_end in tqdm(all_month_ends, desc="L3月截面"):
        month_str = month_end.strftime("%Y-%m")

        # 大盘过滤
        alpha_mult = 1.0
        if hs300_daily is not None:
            alpha_mult = check_market_trend(hs300_daily, month_end)

        # Step 1: 从预计算结果查表
        all_factors = []
        for sym in symbols:
            pre = precomputed.get(sym)
            if pre is None:
                continue

            from factors.precompute import lookup_factors_at
            f = lookup_factors_at(pre, month_end)
            if f is None:
                continue

            f["symbol"] = sym

            # 补齐元数据供 assess_stock 使用（从缓存取，免重复读取）
            daily = daily_all.get(sym)
            if daily is not None:
                daily_cut = daily.loc[:month_end]
                if len(daily_cut) >= 50:
                    f["_daily_cut"] = daily_cut
                    weekly = weekly_all.get(sym)
                    if weekly is not None:
                        f["_weekly_cut"] = weekly.loc[:month_end]
                    monthly = monthly_all.get(sym)
                    if monthly is not None:
                        f["_monthly_cut"] = monthly.loc[:month_end]
                else:
                    continue
            else:
                continue

            all_factors.append(f)

        if not all_factors:
            continue

        # Step 2: 硬约束 + 截面标准化
        factors_df = pd.DataFrame(all_factors)
        factors_df = apply_hard_filters(factors_df)
        if factors_df.empty:
            continue

        meta_cols = ["_daily_cut", "_weekly_cut", "_monthly_cut"]
        meta = {
            c: factors_df[c].tolist() if c in factors_df.columns else []
            for c in meta_cols
        }
        factor_cols = [
            c for c in factors_df.columns
            if c not in meta_cols and c != "symbol"
        ]
        factor_data = factors_df[["symbol"] + factor_cols].copy()

        processed = process_cross_section(factor_data.to_dict("records"))
        if processed.empty:
            continue

        # Step 3: 评分
        scores = aggregate_scores(processed, alpha_multiplier=alpha_mult)

        # Step 4: 逐只过门
        for idx, score_row in scores.iterrows():
            sym = idx

            # 找回原始数据
            daily_cut = None
            weekly_cut = None
            monthly_cut = None
            for i, s in enumerate(factors_df["symbol"]):
                if s == sym:
                    daily_cut = (
                        meta["_daily_cut"][i]
                        if i < len(meta["_daily_cut"])
                        else None
                    )
                    weekly_cut = (
                        meta["_weekly_cut"][i]
                        if i < len(meta["_weekly_cut"])
                        else None
                    )
                    monthly_cut = (
                        meta["_monthly_cut"][i]
                        if i < len(meta["_monthly_cut"])
                        else None
                    )
                    break

            if daily_cut is None:
                continue

            factor_dict = {
                "score": float(score_row["score"]),
                "trend_score": float(score_row.get("trend_score", 0)),
                "alpha_score": float(score_row.get("alpha_score", 0)),
                "risk_score": float(score_row.get("risk_score", 0)),
            }

            # 快速形态乘数（用预计算全历史序列查表，免每月重复算）
            pat = pattern_cache.get(sym)
            if pat is not None:
                from factors.precompute import lookup_pattern_multiplier
                try:
                    factor_dict["_pattern_mult"] = lookup_pattern_multiplier(
                        pat, month_end, daily_cut,
                        weekly_df_slice=weekly_cut,
                    )
                except Exception as e:
                    logger.warning(f"形态乘数查表失败 [{sym}]: {e}")

            try:
                v = assess_stock(
                    sym,
                    daily_df=daily_cut,
                    weekly_df=weekly_cut,
                    monthly_df=monthly_cut,
                    factor_scores=factor_dict,
                )
                all_rows.append(
                    {
                        "symbol": sym,
                        "month": month_str,
                        "passed": v.passed_gate,
                        "score": v.score,
                        "classification": v.classification,
                        "etf_tags": etf_tags_map.get(sym, []),
                    }
                )
            except Exception:
                continue

    result = pd.DataFrame(all_rows)
    if len(result) > 0:
        logger.info(
            f"L3预计算完成: {len(result)} 条记录, "
            f"通过率 {result['passed'].mean():.1%}"
        )
    else:
        logger.info("L3预计算完成: 0条")
    return result


def main():
    parser = argparse.ArgumentParser(description="快速三层漏斗回测")
    parser.add_argument("--quick", action="store_true", help="快速模式 (2023-2024)")
    parser.add_argument("--start", type=str, default=None, help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--n-stocks", type=int, default=500, help="股票数量")
    parser.add_argument("--no-concepts", action="store_true", help="禁用概念板块")
    parser.add_argument("--workers", type=int, default=4, help="并行进程数")
    args = parser.parse_args()

    t_start = time.time()

    # ── 数据加载 ──
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

    # ── 并行预计算 ──
    symbols = store.stock_names
    logger.info(f"开始并行预计算 {len(symbols)} 只股票...")
    precomputed = fast_precompute_all(symbols, cache_dir, n_workers=args.workers)

    if not precomputed:
        logger.error("预计算失败，无有效数据")
        return

    # ── 构建月度时点列表 ──
    all_month_ends: set[pd.Timestamp] = set()
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is not None and len(daily) >= 200:
            ends = daily.resample("ME").groups.keys()
            all_month_ends.update(ends)
    all_month_ends = sorted(all_month_ends)

    pre_start = pd.Timestamp(start) - pd.DateOffset(months=6)
    all_month_ends = [m for m in all_month_ends if pre_start <= m <= pd.Timestamp(end)]

    if not all_month_ends:
        logger.warning("无符合条件的月末时点")
        return

    logger.info(
        f"月末时点: {len(all_month_ends)} 个 "
        f"({all_month_ends[0].date()} ~ {all_month_ends[-1].date()})"
    )

    # ── ETF 标签映射 ──
    from backtest.runner import _build_etf_tags_map

    etf_tags_map = _build_etf_tags_map(store, symbols)
    n_mapped = sum(1 for v in etf_tags_map.values() if v)
    logger.info(f"ETF标签映射: {n_mapped}/{len(symbols)} 只有归属")

    # ── 沪深300 大盘数据 ──
    hs300_daily = store.get_daily("csi300")

    # ── 月度截面循环 ──
    l3_scores = fast_monthly_loop(
        store, precomputed, all_month_ends, symbols,
        hs300_daily=hs300_daily,
        etf_tags_map=etf_tags_map,
    )

    if l3_scores.empty:
        logger.error("L3 预计算无结果")
        return

    # 缓存
    l3_cache_path = (
        cache_dir
        / "precompute"
        / f"l3_scores_{start}_{end}_{args.n_stocks}.parquet"
    )
    l3_cache_path.parent.mkdir(parents=True, exist_ok=True)
    l3_scores.to_parquet(l3_cache_path)
    logger.info(f"L3 评分已缓存: {l3_cache_path}")

    # ── 周回测 ──
    from backtest.runner import run_funnel_backtest, print_report

    concept_indices = None
    if not args.no_concepts:
        try:
            from backtest.data_loader import _load_concept_indices

            concept_indices = _load_concept_indices(cache_dir)
            logger.info(
                f"概念板块已加载: {len(concept_indices)} 个"
            )
        except Exception as e:
            logger.warning(f"概念板块加载失败: {e}")

    result = run_funnel_backtest(
        store,
        l3_scores=l3_scores,
        start=start,
        end=end,
        top_n=10,
        concept_indices=concept_indices,
    )

    print_report(result)

    total_elapsed = time.time() - t_start
    logger.info(f"总耗时: {total_elapsed:.0f}s ({total_elapsed / 60:.1f}min)")


if __name__ == "__main__":
    main()
