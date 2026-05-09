"""全量回测 — 4318 只股票, 2019-01-01 至 2026-05-08, 按年输出结果

用法:
  python scripts/run_full_backtest.py                    # 全量 + 缓存
  python scripts/run_full_backtest.py --no-cache          # 强制重算
  python scripts/run_full_backtest.py --n-stocks 500     # 限制股票数
  python scripts/run_full_backtest.py --output results    # 输出到文件
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path("D:/projects/quant-system")
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.runner import (
    run_funnel_backtest, print_report, _build_etf_tags_map,
    _build_concept_indices,
)
from backtest.precompute_v2 import precompute_all_stocks_v2
from core.logger import get_logger

logger = get_logger("scripts.full_backtest")

# ── 配置 ──
CACHE_DIR = PROJECT_ROOT / "data/raw/_cache/backtest"
START = "2019-01-01"
END = "2026-05-08"
DEFAULT_N_STOCKS = 4318
TOP_N = 10


def main():
    import argparse
    parser = argparse.ArgumentParser(description="全量回测")
    parser.add_argument("--n-stocks", type=int, default=DEFAULT_N_STOCKS)
    parser.add_argument("--no-cache", action="store_true", help="强制重算 L3 评分")
    parser.add_argument("--no-concepts", action="store_true", help="禁用概念板块")
    parser.add_argument("--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    # ── 加载数据 ──
    print(f"加载数据 (n_stocks={args.n_stocks})...")
    from backtest.data_loader import load_all_from_cache
    store = load_all_from_cache(CACHE_DIR, n_stocks=args.n_stocks)
    n_stocks = len(store.stock_names)
    n_indices = len(store.index_names)
    logger.info(f"加载: {n_indices} 指数, {n_stocks} 个股")

    # ── ETF 标签映射 ──
    symbol_etf_map = _build_etf_tags_map(store, store.stock_names)
    n_mapped = sum(1 for v in symbol_etf_map.values() if v)
    logger.info(f"ETF 映射: {n_mapped}/{n_stocks}")

    # ── L3 预计算 (V2: 时间序列 + 月截面) ──
    l3_cache_dir = CACHE_DIR / "precompute_v2"
    l3_cache_path = l3_cache_dir / f"l3_scores_{START}_{END}_{args.n_stocks}.parquet"
    l3_scores = None

    if not args.no_cache and l3_cache_path.exists():
        l3_scores = pd.read_parquet(l3_cache_path)
        logger.info(f"L3 V2 缓存加载: {len(l3_scores)} 条")
    else:
        logger.info(f"L3 V2 预计算中... (时间序列{args.n_stocks}只，约需数分钟)")
        l3_scores = precompute_all_stocks_v2(
            store,
            hs300_daily=store.get_daily("000300"),
            start=START, end=END,
            cache_dir=l3_cache_dir,
        )
        l3_cache_dir.mkdir(parents=True, exist_ok=True)
        l3_scores.to_parquet(l3_cache_path, index=False)
        logger.info(f"L3 V2 缓存保存: {l3_cache_path}")

    if l3_scores is None or l3_scores.empty:
        logger.error("L3 评分计算失败")
        return

    # ── 概念板块 ──
    concept_indices = None
    if not args.no_concepts:
        concept_indices = _build_concept_indices(store, store.stock_names, symbol_etf_map)

    # ── 回测 ──
    print(f"\n回测: {START} → {END}, {n_stocks} 只股票, TopK={TOP_N}")
    result = run_funnel_backtest(
        store=store,
        l3_scores=l3_scores,
        start=START, end=END,
        top_n=TOP_N,
        concept_indices=concept_indices,
    )

    # ── 输出 ──
    print_report(result)

    # 按年详细表格
    if result.yearly:
        print("\n" + "=" * 100)
        print("  按年收益汇总")
        print("=" * 100)
        print(f"  {'年份':^6} {'年化收益':>10} {'最大回撤':>10} {'夏普':>8} {'胜率':>8} {'超额收益':>10} {'L1状态':^16}")
        print("  " + "-" * 90)
        for wy in sorted(result.yearly.keys()):
            ys = result.yearly[wy]
            dominant = max(ys.l1_states, key=ys.l1_states.get) if ys.l1_states else "—"
            win_rate = ys.l3_trade_count / max(ys.l1_checks, 1) if ys.l1_checks > 0 else 0
            print(f"  {wy:^6} {ys.annual_return:>+9.2%} {ys.max_drawdown:>9.2%} "
                  f"{ys.sharpe:>8.3f} {win_rate:>7.1%} {ys.excess_return:>+9.2%} {dominant:^16}")
        print("=" * 100)

    # 总体指标
    print(f"\n总收益: {result.total_return:+.2%} | 年化: {result.annual_return:+.2%} | "
          f"最大回撤: {result.max_drawdown:.2%} | 夏普: {result.sharpe_ratio:.3f} | "
          f"胜率: {result.win_rate:.1%}")

    # 保存结果
    output_dir = PROJECT_ROOT / (args.output or "outputs")
    output_dir.mkdir(parents=True, exist_ok=True)

    # 保存回测结果
    result_path = output_dir / f"full_backtest_{START}_{END}_{args.n_stocks}.parquet"
    annual_rows = []
    for wy, ys in sorted(result.yearly.items()):
        dominant = max(ys.l1_states, key=ys.l1_states.get) if ys.l1_states else "—"
        annual_rows.append({
            "year": wy, "annual_return": ys.annual_return,
            "max_drawdown": ys.max_drawdown, "sharpe": ys.sharpe,
            "excess_return": ys.excess_return,
            "l1_passes": ys.l1_passes, "l1_checks": ys.l1_checks,
            "l2_passes": ys.l2_passes, "l2_checks": ys.l2_checks,
            "l3_avg_picks": ys.l3_avg_picks, "l1_dominant": dominant,
        })
    if annual_rows:
        pd.DataFrame(annual_rows).to_parquet(result_path, index=False)
        logger.info(f"按年结果保存: {result_path}")

    # 保存曲线
    curve_path = output_dir / f"backtest_curve_{START}_{END}_{args.n_stocks}.csv"
    # curve 数据在 run_funnel_backtest 内部，需要重构输出
    logger.info(f"回测完成，输出到: {output_dir}")

    return result


if __name__ == "__main__":
    main()
