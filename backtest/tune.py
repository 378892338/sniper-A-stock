"""三层漏斗参数调优 — Sprint 5

基于已预计算的 L3 评分，快速尝试多种参数组合。

用法:
  python -m backtest.tune
"""
from pathlib import Path

import pandas as pd

from core.logger import get_logger

logger = get_logger("backtest.tune")

CACHE_DIR = Path("data/raw/_cache/backtest")
L3_CACHE = CACHE_DIR / "l3_scores_all.parquet"


def tune():
    from backtest.data_loader import load_all_from_cache
    from backtest.runner import run_funnel_backtest

    logger.info("加载数据...")
    store = load_all_from_cache(CACHE_DIR, n_stocks=0)
    market_weekly = {n: store.get_weekly(n) for n in ["shanghai", "shenzhen", "chinext"] if store.get_weekly(n) is not None}
    market_monthly = {n: store.get_monthly(n) for n in ["shanghai", "shenzhen", "chinext"] if store.get_monthly(n) is not None}
    _ETF_NAMES = ["证券", "银行", "军工", "芯片", "半导体", "新能源车", "光伏", "消费", "医药", "酒", "科技", "有色", "煤炭", "汽车"]
    etf_weekly = {n: store.get_weekly(n) for n in _ETF_NAMES if store.get_weekly(n) is not None}
    bm_daily = store.get_daily("csi300")
    benchmark = bm_daily["close"] if bm_daily is not None else None
    stock_data = {s: store.get_daily(s) for s in store.stock_names}

    if not L3_CACHE.exists():
        logger.error(f"L3 缓存不存在: {L3_CACHE}")
        return

    l3_scores = pd.read_parquet(L3_CACHE)
    logger.info(f"L3 评分加载: {len(l3_scores)} 条")

    # ── 参数网格 ──
    top_n_values = [5, 10, 15, 20, 30]

    results = []

    for top_n in top_n_values:
        logger.info(f"回测: top_n={top_n}")
        r = run_funnel_backtest(
            store=store,
            l3_scores=l3_scores,
            start="2019-01-01", end="2026-04-30",
            top_n=top_n,
        )
        results.append({
            "top_n": top_n,
            "total_return": r.total_return,
            "annual_return": r.annual_return,
            "max_drawdown": r.max_drawdown,
            "sharpe_ratio": r.sharpe_ratio,
            "win_rate": r.win_rate,
            "benchmark_return": r.benchmark_return,
            "excess_return": r.excess_return,
            "empty_position_ratio": r.empty_position_ratio,
            "layer1_pass_rate": r.layer1_pass_rate,
            "layer2_pass_rate": r.layer2_pass_rate,
            "layer3_avg_picks": r.layer3_avg_picks,
        })

    # ── 排序展示 ──
    df = pd.DataFrame(results)
    print("\n" + "=" * 110)
    print("  参数调优结果汇总")
    print("=" * 110)
    print(f"{'top_n':>6} {'总收益':>10} {'年化':>8} {'回撤':>8} {'夏普':>8} {'胜率':>7} {'超额':>9} {'空仓':>7} {'L1通过':>7} {'L2通过':>7}")
    print("-" * 90)
    for _, row in df.iterrows():
        print(f"{row['top_n']:>6.0f} "
              f"{row['total_return']:>+9.2%} {row['annual_return']:>+7.2%} "
              f"{row['max_drawdown']:>7.2%} {row['sharpe_ratio']:>7.3f} "
              f"{row['win_rate']:>6.1%} {row['excess_return']:>+8.2%} "
              f"{row['empty_position_ratio']:>6.1%} "
              f"{row['layer1_pass_rate']:>6.1%} {row['layer2_pass_rate']:>6.1%}")
    print("=" * 90)

    # ── 按夏普排序推荐 ──
    best_sharpe = df.loc[df['sharpe_ratio'].idxmax()]

    print("\n推荐方案:")
    print(f"  最高夏普: top_n={best_sharpe['top_n']:.0f}, "
          f"夏普={best_sharpe['sharpe_ratio']:.3f}, 年化={best_sharpe['annual_return']:+.2%}, "
          f"超额={best_sharpe['excess_return']:+.2%}, 回撤={best_sharpe['max_drawdown']:>.2%}")

    # 保存到文件
    out_path = CACHE_DIR / "tune_results.csv"
    df.to_csv(out_path, index=False)
    logger.info(f"调优结果已保存: {out_path}")

    return df


if __name__ == "__main__":
    tune()
