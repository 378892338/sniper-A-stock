"""L4 狙击手回测诊断工具 — 全链路查询与分析。

用法:
  # 运行带诊断的回测
  python scripts/diagnose_run.py run --n-stocks 500

  # 列出所有历史运行
  python scripts/diagnose_run.py list

  # 查询：为什么某只股票在某日没被买入
  python scripts/diagnose_run.py query why-not --symbol 000001 --date 2023-06-15

  # 查询：入场漏斗转化率
  python scripts/diagnose_run.py query funnel

  # 查询：某只股票的所有交易
  python scripts/diagnose_run.py query trades --symbol 000001

  # 查询：某板块强势周排名
  python scripts/diagnose_run.py query sector --name 电子

  # 查询：市场状态分布
  python scripts/diagnose_run.py query market

  # 查询：完整报告
  python scripts/diagnose_run.py query report

  # 指定运行（默认用最新）
  python scripts/diagnose_run.py query funnel --run 20260513_143000_500
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.diagnostic_engine import (
    DIAGNOSE_BASE_DIR,
    list_runs,
    load_run,
    query_funnel_summary,
    query_why_not_traded,
    query_trades,
    query_market_state,
    query_sector_timeline,
    query_pool_timeline,
    print_report,
)
from core.logger import get_logger

logger = get_logger("scripts.diagnose_run")

CACHE_DIR = PROJECT_ROOT / "data/raw/_cache/backtest"
DIAGNOSE_DIR = CACHE_DIR / "diagnose"


def _resolve_run(run: str | None) -> str | None:
    """解析运行 ID，'latest' 或 None 取最新。"""
    if run and run != "latest":
        return run
    runs = list_runs(DIAGNOSE_DIR)
    if not runs:
        print("没有找到诊断运行记录。先运行: python scripts/diagnose_run.py run")
        return None
    return runs[0]["run_id"]


def cmd_run(args):
    """运行带诊断的回测。"""
    from scripts.run_l4_backtest import load_industry_concept_maps, build_sector_stock_map
    from backtest.data_loader import load_all_from_cache
    from backtest.l4_fractal_backtest import run_l4_backtest, print_l4_report, MAX_LOSS_PER_DAY

    # 加载数据
    print(f"加载数据 (n_stocks={args.n_stocks})...")
    store = load_all_from_cache(CACHE_DIR, n_stocks=args.n_stocks)
    n_stocks = len(store.stock_names)
    print(f"已加载 {len(store.index_names)} 指数, {n_stocks} 个股")

    print("加载行业概念映射...")
    industry_map, concept_map = load_industry_concept_maps()
    sector_stock_map = build_sector_stock_map(store, industry_map, concept_map)

    if not any(sector_stock_map.values()):
        logger.error("板块-股票映射为空")
        return

    diagnose_dir = str(DIAGNOSE_DIR)
    symbols = store.stock_names[:args.n_stocks]

    print(f"\n运行 L4 诊断回测 {args.start} → {args.end} ...")
    print(f"诊断数据目录: {diagnose_dir}")
    print()

    result = run_l4_backtest(
        store=store,
        symbols=symbols,
        sector_stock_map=sector_stock_map,
        start=args.start,
        end=args.end,
        max_stocks=args.max_pool,
        cache_dir=CACHE_DIR,
        diagnose_dir=diagnose_dir,
    )

    print_l4_report(result)
    print(f"\n诊断数据已保存到 {diagnose_dir}")
    print(f"  用以下命令查询: python scripts/diagnose_run.py query funnel")
    return result


def cmd_list(args):
    """列出所有诊断运行。"""
    runs = list_runs(DIAGNOSE_DIR)
    if not runs:
        print("没有找到诊断运行记录。")
        return

    print(f"{'运行ID':25s} {'创建时间':20s} {'参数':30s} {'交易':>6s} {'胜率':>6s} {'收益':>8s} {'回撤':>6s}")
    print("-" * 110)
    for r in runs:
        rid = r["run_id"]
        created = r.get("created_at", "")[:19]
        params = r.get("params", {})
        param_str = f"N={params.get('n_stocks', '?')} P={params.get('max_pool', '?')}"
        nt = str(r.get("n_trades", "?"))
        wr = r.get("win_rate", "?")
        nav_ret = r.get("nav_return", "?")
        mdd = r.get("max_dd", "?")
        print(f"{rid:25s} {created:20s} {param_str:30s} {nt:>6s} {wr:>6s} {nav_ret:>8s} {mdd:>6s}")


def cmd_query(args):
    """运行查询。"""
    run_id = _resolve_run(args.run)
    if run_id is None:
        return

    data = load_run(run_id, DIAGNOSE_DIR)
    print(f"\n运行: {run_id}")
    print(f"参数: {data.get('params', {})}")
    print()

    if args.sub == "report":
        print_report(data, title=f"诊断报告: {run_id}")

    elif args.sub == "funnel":
        funnel = query_funnel_summary(data)
        if funnel.empty:
            print("无入场漏斗数据（诊断模式可能未启用，或候选池为空）")
        else:
            # 追加日期范围的漏斗
            print("入场漏斗转化率（全区间）:")
            print(funnel.to_string(index=False))

    elif args.sub == "why-not":
        if not args.symbol or not args.date:
            print("需要 --symbol 和 --date 参数")
            return
        result = query_why_not_traded(data, args.symbol, args.date)
        print(f"结果: {result['verdict']}")
        for rec in result.get("records", []):
            print(f"  股票: {rec['symbol']} ({rec.get('sector', '')})")
            if rec["passed"]:
                print(f"    ✓ 已买入 (信号: {rec['confirm_count']}/4)")
            else:
                print(f"    ✗ 未通过: {' → '.join(rec.get('blocked_by', ['未知']))}")
                print(f"    信号: {rec['signals']}")
                print(f"    MA20: {rec['ma20_pass']}, 年线: {rec.get('ma250_pass', 'N/A')}, 板块匹配: {rec['sector_match']}")

    elif args.sub == "trades":
        trades = query_trades(data, symbol=args.symbol)
        if trades is None or trades.empty:
            print("无交易记录")
            return
        print(f"交易明细 ({len(trades)} 笔):")
        cols = ["symbol", "entry_date", "exit_date", "net_return", "holding_days", "exit_reason", "sector"]
        print(trades[cols].to_string(index=False))

    elif args.sub == "market":
        ms = query_market_state(data)
        if ms is None:
            print("无市场状态数据")
        else:
            print("市场状态分布:")
            print(ms.to_string(index=False))

    elif args.sub == "sector":
        timeline = query_sector_timeline(data, sector_name=args.name)
        if timeline is None or timeline.empty:
            print("无板块排名数据")
        else:
            print(f"板块强势周排名 {'(' + args.name + ')' if args.name else ''}:")
            print(timeline.to_string(index=False))

    elif args.sub == "pool":
        pool = query_pool_timeline(data, symbol=args.symbol)
        if pool is None or pool.empty:
            print("无 L4 池数据")
        else:
            print(f"L4 候选池 {'(' + args.symbol + ')' if args.symbol else ''}:")
            print(pool.to_string(index=False))

    else:
        print(f"未知查询: {args.sub}")
        print("可用: report, funnel, why-not, trades, market, sector, pool")


def main():
    parser = argparse.ArgumentParser(
        description="L4 狙击手回测诊断工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")

    # run
    p_run = sub.add_parser("run", help="运行带诊断的回测")
    p_run.add_argument("--n-stocks", type=int, default=500)
    p_run.add_argument("--start", type=str, default="2019-01-01")
    p_run.add_argument("--end", type=str, default="2026-05-08")
    p_run.add_argument("--max-pool", type=int, default=8)

    # list
    sub.add_parser("list", help="列出所有诊断运行")

    # query
    p_q = sub.add_parser("query", help="查询诊断数据")
    p_q.add_argument("sub", choices=["report", "funnel", "why-not", "trades", "market", "sector", "pool"])
    p_q.add_argument("--run", default=None, help="运行ID (默认: latest)")
    p_q.add_argument("--symbol", default=None, help="股票代码")
    p_q.add_argument("--date", default=None, help="日期 YYYY-MM-DD")
    p_q.add_argument("--name", default=None, help="板块名")

    args = parser.parse_args()

    if args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "query":
        cmd_query(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
