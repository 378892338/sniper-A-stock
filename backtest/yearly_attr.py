"""分年度归因 — 复用缓存数据，top_n=10

用法: python -m backtest.yearly_attr
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

from backtest.runner import run_funnel_backtest, calc_metrics
from backtest.data_loader import load_all_from_cache
from core.logger import get_logger

logger = get_logger("backtest.yearly_attr")


def main():
    cache_dir = Path("data/raw/_cache/backtest")

    # 加载缓存
    store = load_all_from_cache(cache_dir, n_stocks=4318)
    market_weekly = {n: store.get_weekly(n) for n in ["shanghai", "shenzhen", "chinext"] if store.get_weekly(n) is not None}
    market_monthly = {n: store.get_monthly(n) for n in ["shanghai", "shenzhen", "chinext"] if store.get_monthly(n) is not None}
    _ETF_NAMES = ["证券", "银行", "军工", "芯片", "半导体", "新能源车", "光伏", "消费", "医药", "酒", "科技", "有色", "煤炭", "汽车"]
    etf_weekly = {n: store.get_weekly(n) for n in _ETF_NAMES if store.get_weekly(n) is not None}
    bm_daily = store.get_daily("csi300")
    benchmark = bm_daily["close"] if bm_daily is not None else None
    stock_data = {s: store.get_daily(s) for s in store.stock_names}

    logger.info(f"加载: {len(market_weekly)} 指数, {len(etf_weekly)} ETF, {len(stock_data)} 个股")

    # 复用预计算的 L3 评分
    l3_path = cache_dir / "l3_scores_all.parquet"
    if l3_path.exists():
        l3_scores = pd.read_parquet(l3_path)
        logger.info(f"复用 L3 缓存: {len(l3_scores)} 条")
    else:
        from backtest.runner import precompute_all_stocks
        l3_scores = precompute_all_stocks(store)
        l3_scores.to_parquet(l3_path, index=False)

    top_n = 10

    # 分年度回测
    years = list(range(2019, 2027))
    results = []

    print(f"\n{'Year':>6} {'累计':>10} {'年化':>8} {'回撤':>8} {'夏普':>7} {'胜率':>7} {'超额':>8} {'空仓':>7} {'L1%':>6} {'调仓':>5} {'交易':>5}")
    print("-" * 95)

    for y in years:
        start = f"{y}-01-01"
        end = f"{y}-12-31" if y < 2026 else "2026-04-30"

        r = run_funnel_backtest(
            store=store,
            l3_scores=l3_scores,
            start=start, end=end,
            top_n=top_n,
        )

        trades_count = len(r.trades)
        # 估算总交易笔数 = 调仓次数 × 平均持仓数
        total_deals = int(trades_count * r.layer3_avg_picks) if r.layer3_avg_picks else 0

        print(f"{y:>6} {r.total_return:>+9.1%} {r.annual_return:>+7.1%} "
              f"{r.max_drawdown:>-7.1%} {r.sharpe_ratio:>6.3f} {r.win_rate:>6.1%} "
              f"{r.excess_return:>+7.1%} {r.empty_position_ratio:>6.1%} "
              f"{r.layer1_pass_rate:>5.1%} {trades_count:>5} {total_deals:>5}")

        results.append({
            "year": y,
            "total_return": r.total_return,
            "annual_return": r.annual_return,
            "max_drawdown": r.max_drawdown,
            "sharpe": r.sharpe_ratio,
            "win_rate": r.win_rate,
            "excess_return": r.excess_return,
            "empty_pct": r.empty_position_ratio,
            "l1_pass": r.layer1_pass_rate,
            "trades": trades_count,
            "deals": total_deals,
        })

    # 汇总统计
    df = pd.DataFrame(results)
    print(f"\n{'均值':>6} {df['total_return'].mean():>+9.1%} {df['annual_return'].mean():>+7.1%} "
          f"{df['max_drawdown'].mean():>-7.1%} {df['sharpe'].mean():>6.3f} {df['win_rate'].mean():>6.1%} "
          f"{df['excess_return'].mean():>+7.1%} {df['empty_pct'].mean():>6.1%} "
          f"{df['l1_pass'].mean():>5.1%} {int(df['trades'].sum()):>5} {int(df['deals'].sum()):>5}")
    print(f"{'标准差':>6} {df['total_return'].std():>+9.1%} {df['annual_return'].std():>+7.1%} "
          f"{df['max_drawdown'].std():>-7.1%} {df['sharpe'].std():>6.3f}")

    # 盈利年数
    pos_years = (df["total_return"] > 0).sum()
    pos_excess = (df["excess_return"] > 0).sum()
    print(f"\n正收益年: {pos_years}/{len(years)}  |  正超额年: {pos_excess}/{len(years)}")

    # 收益集中度
    total_cum = (1 + df["total_return"]).prod() - 1
    top2 = df.nlargest(2, "total_return")
    top2_contrib = (1 + top2["total_return"]).prod() - 1
    print(f"全期累计: {total_cum:+.1%}  |  最佳两年贡献: {top2_contrib:+.1%} ({top2_contrib/total_cum*100:.0f}%)")
    print(f"最佳年份: {int(top2.iloc[0]['year'])} ({top2.iloc[0]['total_return']:+.1%}), "
          f"次佳: {int(top2.iloc[1]['year'])} ({top2.iloc[1]['total_return']:+.1%})")

    print("-" * 95)


if __name__ == "__main__":
    main()
