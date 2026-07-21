"""L4 回测亏损分析 — 从最大回撤倒查亏损原因"""

import sys
from pathlib import Path

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.l4_fractal_backtest import run_l4_backtest, L4FractalResult
from core.logger import get_logger

logger = get_logger("scripts.analyze_losses")

CACHE_DIR = PROJECT_ROOT / "data/raw/_cache/backtest"
START = "2019-01-01"
END = "2026-05-08"


def analyze_trades(result: L4FractalResult, output_dir: Path):
    """对所有交易记录进行深度的亏损原因分析。"""
    trades = result.trades
    if not trades:
        print("无交易记录")
        return

    df = pd.DataFrame([{
        "symbol": t.symbol,
        "entry_date": t.entry_date,
        "exit_date": t.exit_date,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "net_return": t.net_return,
        "holding_days": t.holding_days,
        "exit_reason": t.exit_reason,
        "pattern": t.pattern,
        "sector": t.sector,
        "year": t.entry_date.year,
    } for t in trades])

    # 保存全量交易明细
    df.to_parquet(output_dir / "trade_details.parquet")
    print(f"交易明细已保存: {len(df)} 条")

    # ════════════════════════════════════════
    # 1. 按退出原因分析亏损
    # ════════════════════════════════════════
    print("\n" + "=" * 70)
    print("1. 按退出原因分析")
    print("=" * 70)
    by_reason = df.groupby("exit_reason").agg(
        交易次数=("net_return", "count"),
        胜率=("net_return", lambda x: (x > 0).mean()),
        平均收益=("net_return", "mean"),
        总收益=("net_return", "sum"),
        中位收益=("net_return", "median"),
        平均持仓=("holding_days", "mean"),
    ).sort_values("交易次数", ascending=False)
    print(by_reason.to_string())

    # ════════════════════════════════════════
    # 2. 按退出原因 × 年份 的亏损分布
    # ════════════════════════════════════════
    print("\n" + "=" * 70)
    print("2. 退出原因 × 年份 — 亏损交易占比")
    print("=" * 70)
    loss_by_reason_year = df[df["net_return"] < 0].groupby(["year", "exit_reason"]).agg(
        亏损次数=("net_return", "count"),
        平均亏损=("net_return", "mean"),
        总亏损=("net_return", "sum"),
    ).sort_values(["year", "亏损次数"], ascending=[True, False])
    print(loss_by_reason_year.to_string())

    # ════════════════════════════════════════
    # 3. 2021 年深度分析（最大回撤年份）
    # ════════════════════════════════════════
    print("\n" + "=" * 70)
    print("3. 2021 年深度分析（最大亏损年份）")
    print("=" * 70)
    d2021 = df[df["year"] == 2021]
    print(f"  2021年交易: {len(d2021)} 笔")
    print(f"  胜率: {(d2021['net_return'] > 0).mean():.1%}")
    print(f"  平均收益: {d2021['net_return'].mean():+.2%}")
    print(f"  中位收益: {d2021['net_return'].median():+.2%}")
    print(f"  总收益: {d2021['net_return'].sum():+.2%}")

    # 2021逐月
    d2021["month"] = d2021["entry_date"].dt.month
    monthly = d2021.groupby("month").agg(
        交易次数=("net_return", "count"),
        胜率=("net_return", lambda x: (x > 0).mean()),
        平均收益=("net_return", "mean"),
        总收益=("net_return", "sum"),
    )
    print(f"\n  2021 逐月:")
    print(monthly.to_string())

    # 2021按退出原因
    print(f"\n  2021 退出原因:")
    by_reason_2021 = d2021.groupby("exit_reason").agg(
        交易次数=("net_return", "count"),
        胜率=("net_return", lambda x: (x > 0).mean()),
        平均收益=("net_return", "mean"),
        总收益=("net_return", "sum"),
        平均持仓=("holding_days", "mean"),
    ).sort_values("交易次数", ascending=False)
    print(by_reason_2021.to_string())

    # 2021亏损最大的10笔
    print(f"\n  2021 Top 10 亏损交易:")
    worst_2021 = d2021.sort_values("net_return").head(10)
    for _, r in worst_2021.iterrows():
        print(f"  {r['symbol']:>8} {str(r['entry_date'])[:10]}→{str(r['exit_date'])[:10]} "
              f"{r['holding_days']:>3}d {r['net_return']:>+7.2%} "
              f"[{r['exit_reason']}] {r['sector']}")

    # ════════════════════════════════════════
    # 4. 按板块分析
    # ════════════════════════════════════════
    print("\n" + "=" * 70)
    print("4. 按板块分析亏损")
    print("=" * 70)
    by_sector = df.groupby("sector").agg(
        交易次数=("net_return", "count"),
        胜率=("net_return", lambda x: (x > 0).mean()),
        平均收益=("net_return", "mean"),
        总收益=("net_return", "sum"),
    ).sort_values("交易次数", ascending=False)
    print(by_sector[by_sector["交易次数"] >= 5].to_string())

    # 亏损最严重的板块
    print(f"\n  亏损最严重的板块（按总收益升序）:")
    worst_sectors = by_sector.sort_values("总收益").head(10)
    print(worst_sectors.to_string())

    # ════════════════════════════════════════
    # 5. 持仓天数与收益关系
    # ════════════════════════════════════════
    print("\n" + "=" * 70)
    print("5. 持仓天数与收益关系")
    print("=" * 70)
    holding_bins = [0, 2, 5, 10, 15, 20, 30, 60, 999]
    holding_labels = ["1-2d", "3-5d", "6-10d", "11-15d", "16-20d", "21-30d", "31-60d", "60d+"]
    df["holding_group"] = pd.cut(df["holding_days"], bins=holding_bins, labels=holding_labels, right=True)
    by_holding = df.groupby("holding_group", observed=True).agg(
        交易次数=("net_return", "count"),
        胜率=("net_return", lambda x: (x > 0).mean()),
        平均收益=("net_return", "mean"),
        中位收益=("net_return", "median"),
        总收益=("net_return", "sum"),
    )
    print(by_holding.to_string())

    # ════════════════════════════════════════
    # 6. L4池规模 vs 收益
    # ════════════════════════════════════════
    print("\n" + "=" * 70)
    print("6. 每日持仓数量分布")
    print("=" * 70)
    if hasattr(result, "weekly_l4_pool") and result.weekly_l4_pool:
        pool_sizes = [len(v) for v in result.weekly_l4_pool.values()]
        print(f"  L4池规模: 均值={np.mean(pool_sizes):.1f} "
              f"中位={np.median(pool_sizes):.1f} "
              f"最大={max(pool_sizes)} 最小={min(pool_sizes)}")
        h, b = np.histogram(pool_sizes, bins=range(0, max(pool_sizes) + 2))
        for i in range(len(h)):
            if h[i] > 0:
                print(f"  {b[i]:.0f}只持仓: {h[i]} 周")

    # ════════════════════════════════════════
    # 7. 亏损交易的退出原因时序分布（市场转弱是否集中）
    # ════════════════════════════════════════
    print("\n" + "=" * 70)
    print("7. 市场转弱清仓的触发频率")
    print("=" * 70)
    bear_exits = df[df["exit_reason"] == "市场转弱清仓"]
    if len(bear_exits) > 0:
        bear_by_year = bear_exits.groupby("year").agg(
            次数=("net_return", "count"),
            占当年交易比=("net_return", lambda x: len(x) / len(df[df["year"] == x.name]) if len(df[df["year"] == x.name]) > 0 else 0),
            总亏损=("net_return", "sum"),
        )
        print(bear_by_year.to_string())

    # ════════════════════════════════════════
    # 8. 当日亏损上限触发统计
    # ════════════════════════════════════════
    print("\n" + "=" * 70)
    print("8. 各退出原因平均亏损幅度")
    print("=" * 70)
    loss_by_reason = df[df["net_return"] < 0].groupby("exit_reason").agg(
        亏损次数=("net_return", "count"),
        平均亏损=("net_return", "mean"),
        中位亏损=("net_return", "median"),
        最大亏损=("net_return", "min"),
        亏损标准差=("net_return", "std"),
    ).sort_values("亏损次数", ascending=False)
    print(loss_by_reason.to_string())


def main():
    import argparse
    parser = argparse.ArgumentParser(description="L4 回测亏损分析")
    parser.add_argument("--n-stocks", type=int, default=200, help="测试股票数")
    parser.add_argument("--skip-backtest", action="store_true", help="跳过回测，直接分析已有的交易明细")
    args = parser.parse_args()

    output_dir = PROJECT_ROOT / "data/raw/_cache/backtest/analysis"
    output_dir.mkdir(parents=True, exist_ok=True)

    trade_file = output_dir / "trade_details.parquet"

    if args.skip_backtest and trade_file.exists():
        print(f"从缓存加载交易明细: {trade_file}")
        df = pd.read_parquet(trade_file)
        # 重建 result 结构
        result = L4FractalResult()
        result.trades = []
        for _, r in df.iterrows():
            from backtest.l4_fractal_backtest import TradeRecord
            t = TradeRecord(
                symbol=r["symbol"],
                entry_date=r["entry_date"],
                exit_date=r["exit_date"],
                entry_price=r["entry_price"],
                exit_price=r["exit_price"],
                net_return=r["net_return"],
                holding_days=int(r["holding_days"]),
                exit_reason=r["exit_reason"],
                pattern=r.get("pattern", ""),
                sector=r.get("sector", ""),
            )
            result.trades.append(t)
        result.n_trades = len(result.trades)
        result.win_rate = (df["net_return"] > 0).mean()
        result.total_return = 0.0  # 无法恢复
    else:
        print("运行全量回测...")
        from scripts.run_l4_backtest import load_industry_concept_maps, build_sector_stock_map
        from backtest.data_loader import load_all_from_cache

        store = load_all_from_cache(CACHE_DIR, n_stocks=args.n_stocks)
        n_stocks = len(store.stock_names)
        print(f"已加载 {len(store.index_names)} 指数, {n_stocks} 个股")

        industry_map, concept_map = load_industry_concept_maps()
        sector_stock_map = build_sector_stock_map(store, industry_map, concept_map)

        if not any(sector_stock_map.values()):
            logger.error("板块-股票映射为空")
            return

        symbols = store.stock_names[:args.n_stocks]

        result = run_l4_backtest(
            store=store,
            symbols=symbols,
            sector_stock_map=sector_stock_map,
            start=START,
            end=END,
            max_stocks=8,
            cache_dir=CACHE_DIR,
        )

    # 执行深度分析
    analyze_trades(result, output_dir)

    # 打印简短摘要
    if result.n_trades > 0:
        df = pd.DataFrame([{
            "net_return": t.net_return,
            "exit_reason": t.exit_reason,
            "sector": t.sector,
            "holding_days": t.holding_days,
            "year": t.entry_date.year,
        } for t in result.trades])

        print("\n" + "=" * 70)
        print("分析摘要")
        print("=" * 70)

        # 按退出原因：亏损贡献占比
        total_loss = abs(df[df["net_return"] < 0]["net_return"].sum())
        by_reason_loss = df[df["net_return"] < 0].groupby("exit_reason")["net_return"].sum()
        print("\n亏损贡献占比（按退出原因）:")
        for reason, loss in sorted(by_reason_loss.items(), key=lambda x: x[1]):
            print(f"  {reason:16s}: {abs(loss):>+7.2%} ({abs(loss)/total_loss:>5.1%})")

        # 亏损分布
        print(f"\n亏损分布:")
        for pct in [0.01, 0.02, 0.03, 0.05, 0.10]:
            n = (df["net_return"] < -pct).sum()
            print(f"  亏损>{pct:.0%}: {n} 笔 ({n/len(df):.1%})")


if __name__ == "__main__":
    main()
