"""Sniper 回测 + 深度分析（L2 并行预计算，参数迭代秒级）"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time
import json
import argparse
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import Counter, defaultdict

from sniper.data_router import DataRouter
from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics, print_metrics_table
from sniper.layers.l1_sector import SectorScorer
from sniper.l2_cache import load_cache, save_top_stocks, get_top_stocks, cache_info, clear_cache

BANNER = r"""
   ___      _          ___
  / _ \ ___|o|_ _  ___| _ \ __ _ _ _  _ _  __ _ _ __
 | (_) | _ \ | ' \/ -_)  _/ _` | '_|| '_|/ _` | '_ \
  \___/|  _/_|_||_\___|_| \__,_|_|  |_|  \__,_| .__/
       |_|                                     |_|
"""


# ── 模块级 worker 函数（Windows multiprocessing 需要可 pickle）──

def _worker_compute_l2(date_sectors_tuple):
    """单个工作进程：计算指定日期的 L2 评分。"""
    date, top_sectors = date_sectors_tuple
    # 每个进程独立创建 router（SQLite 连接不能跨进程共享）
    router = DataRouter()
    from sniper.layers.l2_stock import StockScorer
    scorer = StockScorer(router)
    try:
        result = scorer.top_stocks(date, top_sectors)
        return date, result
    except Exception:
        return date, []


def _worker_compute_chunk(chunk):
    """单个工作进程：计算一批日期的 L2 评分。"""
    import os
    os.environ["SNIPER_LOG_LEVEL"] = "WARNING"
    router = DataRouter()
    from sniper.layers.l2_stock import StockScorer
    scorer = StockScorer(router)
    results = {}
    for date, top_sectors in chunk:
        try:
            results[date] = scorer.top_stocks(date, top_sectors)
        except Exception:
            results[date] = []
    return results


# ── 辅助函数 ──

def convert(obj):
    if isinstance(obj, dict):
        return {k: convert(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert(v) for v in obj]
    elif isinstance(obj, float):
        return round(obj, 4)
    return obj


def analyze(trades, daily_values):
    buys = [t for t in trades if t.get("action") == "BUY"]
    sells = [t for t in trades if t.get("action") == "SELL"]

    print(f"\n{'='*60}")
    print(f"交易统计")
    print(f"{'='*60}")
    print(f"买入: {len(buys)} 笔")
    print(f"卖出: {len(sells)} 笔")

    exit_reasons = Counter()
    for t in sells:
        exit_reasons[t.get("reason", "未知")] += 1
    print(f"\n出场原因分布:")
    for reason, count in exit_reasons.most_common():
        print(f"  {reason}: {count} 笔")

    monthly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in sells:
        month = t.get("date", "")[:7]
        pnl = t.get("pnl", 0)
        monthly[month]["pnl"] += pnl
        monthly[month]["trades"] += 1
        if pnl > 0:
            monthly[month]["wins"] += 1
    print(f"\n月度表现:")
    print(f"{'月份':<10} {'交易':>5} {'胜率':>8} {'总PnL':>12}")
    print(f"{'-'*40}")
    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
        print(f"{month:<10} {m['trades']:>5} {wr:>7.1f}% {m['pnl']:>10.0f}")

    sector_pnl = defaultdict(float)
    sector_trades = defaultdict(int)
    for t in sells:
        sec = t.get("sector", "未知")
        sector_pnl[sec] += t.get("pnl", 0)
        sector_trades[sec] += 1
    print(f"\n板块表现(Top 10):")
    print(f"{'板块':<15} {'交易':>5} {'总PnL':>12}")
    print(f"{'-'*35}")
    for sec, pnl in sorted(sector_pnl.items(), key=lambda x: abs(x[1]), reverse=True)[:10]:
        print(f"{sec:<15} {sector_trades[sec]:>5} {pnl:>10.0f}")

    win_trades = [t for t in sells if t.get("pnl", 0) > 0]
    loss_trades = [t for t in sells if t.get("pnl", 0) <= 0]
    if win_trades and loss_trades:
        avg_win = sum(t["pnl"] for t in win_trades) / len(win_trades)
        avg_loss = abs(sum(t["pnl"] for t in loss_trades) / len(loss_trades))
        wr = len(win_trades) / len(sells)
        rr = avg_win / avg_loss if avg_loss > 0 else 0
        print(f"\n盈亏分析:")
        print(f"  盈利交易: {len(win_trades)} 笔 ({wr*100:.1f}%)")
        print(f"  亏损交易: {len(loss_trades)} 笔")
        print(f"  平均盈利: {avg_win:.0f}")
        print(f"  平均亏损: {avg_loss:.0f}")
        print(f"  盈亏比: {rr:.2f}")
        print(f"  期望值 E = W*R-(1-W): {wr*rr-(1-wr):.3f}")

    hold_days = [t.get("hold_days", 0) for t in sells if t.get("hold_days", 0) > 0]
    if hold_days:
        print(f"\n持仓天数: 最短={min(hold_days)} 最长={max(hold_days)} 平均={sum(hold_days)/len(hold_days):.1f}")

    print(f"\nTop 10 亏损交易:")
    for t in sorted(loss_trades, key=lambda x: x.get("pnl", 0))[:10]:
        print(f"  {t.get('symbol','?')} PnL={t.get('pnl',0):.0f} 原因={t.get('reason','?')} {t.get('date','?')}")

    print(f"\nTop 10 盈利交易:")
    for t in sorted(win_trades, key=lambda x: x.get("pnl", 0), reverse=True)[:10]:
        print(f"  {t.get('symbol','?')} PnL={t.get('pnl',0):.0f} 原因={t.get('reason','?')} {t.get('date','?')}")

    return {
        "exit_reasons": dict(exit_reasons.most_common()),
        "monthly": {k: dict(v) for k, v in sorted(monthly.items())},
        "win_rate": round(wr, 4),
        "profit_factor": round(sum(t["pnl"] for t in win_trades) / max(abs(sum(t["pnl"] for t in loss_trades)), 1), 4),
        "expectancy": round(wr * rr - (1 - wr), 4),
    }


# ── 核心流程 ──

def precompute_parallel(dates, max_workers=None):
    """并行预计算 L0 → L1 → L2，结果写缓存。

    阶段 1 (串行, ~4min): L0+L1 板块评分
    阶段 2 (并行, ~20min): L2 个股评分 (8 进程)
    """
    if max_workers is None:
        max_workers = min(multiprocessing.cpu_count(), 8)

    router = DataRouter()

    # ── 阶段 1: L1 板块评分（串行）──
    print("\n--- 阶段 1/2: L1 板块评分（串行）---")
    sector_scorer = SectorScorer(router)
    top_sectors_map: dict[str, list[str]] = {}
    t1 = time.time()
    for i, date in enumerate(dates):
        sectors = sector_scorer.top_sw2_sectors(date, single_layer=False)
        if sectors:
            top_sectors_map[date] = sectors
        if (i + 1) % 50 == 0:
            print(f"  L1: {i+1}/{len(dates)} ({time.time()-t1:.0f}s)")
    print(f"L1 完成: {len(top_sectors_map)}/{len(dates)} 天有板块, 耗时 {time.time()-t1:.0f}s")

    # ── 阶段 2: L2 个股评分（并行）──
    cached = load_cache()
    todo = [(d, top_sectors_map[d]) for d in dates
            if d not in cached and d in top_sectors_map]

    if not todo:
        print(f"\n所有 {len(dates)} 天 L2 已缓存，跳过阶段 2")
        return

    print(f"\n--- 阶段 2/2: L2 个股评分（{max_workers} 进程并行）---")
    print(f"需计算 {len(todo)}/{len(dates)} 天")

    # 分块，每进程处理一批日期
    chunk_size = max(1, len(todo) // max_workers)
    chunks = [todo[i:i + chunk_size] for i in range(0, len(todo), chunk_size)]
    print(f"分 {len(chunks)} 块，每块 ~{chunk_size} 天")

    t2 = time.time()
    completed = 0
    total = len(todo)

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker_compute_chunk, chunk): chunk for chunk in chunks}

        for future in as_completed(futures):
            results = future.result()
            for date, top_stocks in results.items():
                save_top_stocks(date, top_stocks)
            completed += len(results)
            elapsed = time.time() - t2
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (total - completed) / rate if rate > 0 else 0
            print(f"  L2: {completed}/{total} ({elapsed:.0f}s, {rate:.1f}天/s, ETA {eta:.0f}s)")

    print(f"L2 并行计算完成，耗时 {time.time()-t2:.0f}s")
    print(f"总预计算耗时: {time.time()-t1:.0f}s")


def run_backtest(start_date, end_date):
    """使用缓存的 L2 数据运行回测（秒级）。"""
    router = DataRouter()
    engine = BacktestEngine(router)

    # 缓存装饰器
    original_top_stocks = engine.stock_scorer.top_stocks

    def cached_top_stocks(date, top_sectors):
        cached = get_top_stocks(date)
        if cached is not None:
            # 缓存来自预计算（top_n=5），需过滤只保留请求板块的股票
            sector_symbols: set[str] = set()
            for sec in top_sectors:
                s = engine.stock_scorer.get_sector_stocks(sec)
                if s:
                    sector_symbols.update(s)
            if sector_symbols:
                filtered = [c for c in cached if c["symbol"] in sector_symbols]
                if filtered:
                    return filtered
        result = original_top_stocks(date, top_sectors)
        save_top_stocks(date, result)
        return result

    engine.stock_scorer.top_stocks = cached_top_stocks

    t0 = time.time()
    result = engine.run(start_date, end_date)
    elapsed = time.time() - t0

    trades = result.get("trades", [])
    daily_values = result.get("daily_values", [])
    initial_capital = 1_000_000
    metrics = calculate_metrics(daily_values, trades, initial_capital)

    print(f"\n{'='*50}")
    print(f"回测完成. 耗时: {elapsed:.0f}s")
    print(f"最终资金: {result['final_capital']:.2f}")
    print(f"总收益率: {result['total_return']*100:.2f}%")
    print(f"交易笔数: {len(trades)}")
    print(f"NAV 点数: {len(daily_values)}")
    print_metrics_table(metrics)

    analysis = analyze(trades, daily_values)

    full = {
        "elapsed": round(elapsed, 1),
        "final_capital": round(result["final_capital"], 2),
        "total_return": round(result["total_return"], 4),
        "metrics": {k: round(v, 4) if isinstance(v, float) else v for k, v in metrics.items()},
        "analysis": analysis,
        "trades": [convert(t) for t in trades],
        "daily_values": [convert(d) for d in daily_values],
    }
    with open("backtest_full_2024.json", "w") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)
    print(f"\n完整数据已保存至 backtest_full_2024.json")
    return full


def main():
    parser = argparse.ArgumentParser(description="Sniper 回测 + 并行预计算")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--precompute", action="store_true", help="并行预计算 L2 缓存")
    parser.add_argument("--backtest", action="store_true", help="使用缓存运行回测")
    parser.add_argument("--clear-cache", action="store_true", help="清空 L2 缓存")
    parser.add_argument("--workers", type=int, default=0, help="并行进程数 (0=自动)")
    args = parser.parse_args()

    # 默认行为：没有指定 --precompute/--backtest 时两者都做
    do_precompute = args.precompute
    do_backtest = args.backtest
    if not do_precompute and not do_backtest:
        do_precompute = True
        do_backtest = True

    print(BANNER)
    print(f"回测区间: {args.start} ~ {args.end}")

    if args.clear_cache:
        clear_cache()
        print("L2 缓存已清空")

    router = DataRouter()
    cal = router.get_trading_dates(args.start, args.end)
    if cal.empty:
        print("无交易日数据")
        return
    dates = sorted(cal["date"].tolist())
    print(f"交易日数: {len(dates)}")

    info = cache_info()
    print(f"L2 缓存: {info['total_dates']}/{len(dates)} 天 ({info['date_range']})")

    workers = args.workers if args.workers > 0 else None

    if do_precompute:
        print(f"\n{'='*50}")
        print("阶段 1: 并行预计算 L0 → L1 → L2")
        print(f"{'='*50}")
        precompute_parallel(dates, max_workers=workers)

    if do_backtest:
        print(f"\n{'='*50}")
        print("阶段 2: 使用缓存运行回测")
        print(f"{'='*50}")
        run_backtest(args.start, args.end)


if __name__ == "__main__":
    main()
