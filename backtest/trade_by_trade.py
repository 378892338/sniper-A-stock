"""单票逐笔回测 — 买入信号→持有→卖出→平仓

与 run_funnel_backtest 相同策略逻辑，但 P&L 按逐笔交易计算:
  买入: 选中股票的日收盘价
  卖出: 调仓日的日收盘价
  单笔收益 = 卖出价 / 买入价 - 1
  多笔累乘 = 全期净值曲线

用法:
  python -m backtest.trade_by_trade
  python -m backtest.trade_by_trade --quick
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from tqdm import tqdm

from backtest.runner import (
    evaluate_l1, evaluate_l2, precompute_all_stocks
)
from backtest.data_loader import load_all_from_cache, get_benchmark
from data.store import DataStore
from core.logger import get_logger

logger = get_logger("backtest.trade_by_trade")

# 回测用 ETF 和市场列表
_ETF_NAMES = ["证券", "银行", "军工", "新能源车", "消费", "医药", "酒", "有色", "煤炭"]
_MARKET_NAMES = ["shanghai", "shenzhen", "chinext"]


def _trade_date(dt: pd.Timestamp) -> pd.Timestamp:
    """把周日/周末调仓日映射到上一个交易日（周五）。"""
    if dt.weekday() >= 5:
        return dt - pd.Timedelta(days=dt.weekday() - 4)
    return dt


def run_trade_backtest(
    store: DataStore,
    l3_scores: pd.DataFrame | None = None,
    start="2019-01-01", end="2024-12-31", top_n=10,
    trade_cost=0.0001,
):
    """逐笔交易回测（持有至卖出信号模式）。

    买入信号: L1+L2通过 + L3评分进入 top N → 买入
    卖出信号: L1/L2不通过 → 全部卖出；或 L3评分跌出 top N → 卖出该只
    无信号: 持续持有（不重复开平仓）
    """
    from backtest.runner import BacktestResult

    # 从 DataStore 构建所需 dict
    market_weekly = {n: store.get_weekly(n) for n in _MARKET_NAMES if store.get_weekly(n) is not None}
    market_monthly = {n: store.get_monthly(n) for n in _MARKET_NAMES if store.get_monthly(n) is not None}
    etf_weekly = {n: store.get_weekly(n) for n in _ETF_NAMES if store.get_weekly(n) is not None}
    etf_daily = {n: store.get_daily(n) for n in _ETF_NAMES if store.get_daily(n) is not None}
    benchmark = get_benchmark(store)

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

    curve = [1.0]
    trades = []
    holdings = {}
    prev_week = pd.Timestamp(start)

    def _close_trade(t, sym, trade_ds):
        sdf = store.get_daily(sym)
        if sdf is not None and trade_ds in sdf.index:
            exit_price = float(sdf.loc[trade_ds, "close"])
            exit_net = exit_price * (1 - trade_cost)
            t["exit_date"] = trade_ds
            t["exit_price"] = exit_price
            t["return"] = exit_net / t["entry_price"] - 1
            return True
        return False

    def _fill_flat_week():
        nonlocal empty_days
        week_days = pd.bdate_range(prev_week, w_date)
        if len(week_days) > 0:
            week_days = week_days[1:]
        for _ in week_days:
            curve.append(curve[-1])
            empty_days += 1

    for w_idx, w_date in enumerate(tqdm(weekly_dates, desc="回测")):
        w_str = str(w_date)[:10]
        trade_dt = _trade_date(w_date)
        trade_ds = str(trade_dt)[:10]

        # ── L1 ──
        layer1_checks += 1
        l1_ok, pos_pct = evaluate_l1(market_weekly, market_monthly, w_str)
        if l1_ok:
            layer1_passes += 1

        if not l1_ok:
            for sym in list(holdings.keys()):
                _close_trade(holdings[sym], sym, trade_ds)
            holdings.clear()
            _fill_flat_week()
            prev_week = w_date
            continue

        # ── L2 ──
        layer2_checks += 1
        l2_ok, strong_sectors = evaluate_l2(etf_weekly, benchmark, etf_daily, w_str)
        if l2_ok:
            layer2_passes += 1

        if not l2_ok or not strong_sectors:
            for sym in list(holdings.keys()):
                _close_trade(holdings[sym], sym, trade_ds)
            holdings.clear()
            _fill_flat_week()
            prev_week = w_date
            continue

        # ── L3 ──
        month_key = (pd.Timestamp(w_str) - pd.DateOffset(months=1)).strftime("%Y-%m")
        month_scores = l3_scores[
            (l3_scores["month"] == month_key) & (l3_scores["passed"])
        ] if l3_scores is not None else pd.DataFrame()

        if month_scores.empty:
            pass
        else:
            top_stocks = month_scores.nlargest(top_n, "score")
            target_set = set(top_stocks["symbol"].tolist())
            current_set = set(holdings.keys())

            for sym in current_set - target_set:
                _close_trade(holdings[sym], sym, trade_ds)
                del holdings[sym]

            for sym in target_set - current_set:
                sdf = store.get_daily(sym)
                if sdf is not None and trade_ds in sdf.index:
                    entry_price = float(sdf.loc[trade_ds, "close"])
                    entry_cost = entry_price * (1 + trade_cost)
                    t = {
                        "symbol": sym,
                        "entry_date": trade_ds,
                        "entry_price": entry_cost,
                        "exit_date": None,
                        "exit_price": None,
                        "return": None,
                    }
                    holdings[sym] = t
                    trades.append(t)

        # 计算该周每日收益
        week_days = pd.bdate_range(prev_week, w_date)
        if w_idx > 0 and len(week_days) > 0:
            week_days = week_days[1:]
        for d in week_days:
            ds = str(d)[:10]
            if holdings:
                rets = []
                for sym, t in holdings.items():
                    sdf = store.get_daily(sym)
                    if sdf is not None and ds in sdf.index:
                        idx = sdf.index.get_loc(ds)
                        if idx > 0:
                            prev_c = float(sdf["close"].iloc[idx - 1])
                            cur_c = float(sdf["close"].iloc[idx])
                            if prev_c > 0:
                                rets.append(cur_c / prev_c - 1)
                if rets:
                    curve.append(curve[-1] * (1 + np.mean(rets) * pos_pct))
                else:
                    curve.append(curve[-1])
                    empty_days += 1
            else:
                curve.append(curve[-1])
                empty_days += 1

        prev_week = w_date

    # 最后：所有剩仓按最终交易日平仓
    last_dt = _trade_date(weekly_dates[-1])
    all_dates = sorted(set().union(
        *[set(store.get_daily(sym).index) for sym in store.stock_names if store.get_daily(sym) is not None]
    ))
    final_dates = [d for d in all_dates if d >= last_dt]
    final_ds = str(final_dates[-1])[:10] if final_dates else str(last_dt)[:10]

    for sym in list(holdings.keys()):
        _close_trade(holdings[sym], sym, final_ds)
    holdings.clear()

    # ── 指标计算 ──
    closed_trades = [t for t in trades if t["return"] is not None]
    rets_arr = [t["return"] for t in closed_trades]
    n_trades = len(rets_arr)

    pc = pd.Series(curve).pct_change().dropna()
    total_return = float((1 + pc).prod() - 1) if not pc.empty else 0
    total_days = len(pc)
    years = max(total_days / 252, 0.01)
    annual_return = (1 + total_return) ** (1 / years) - 1
    annual_vol = pc.std() * np.sqrt(252) if not pc.empty else 0
    sharpe = (annual_return - 0.02) / annual_vol if annual_vol > 0 else 0
    cummax = (1 + pc).cumprod().cummax() if not pc.empty else pd.Series([1])
    drawdown = (1 + pc).cumprod() / cummax - 1 if not pc.empty else pd.Series([0])
    max_dd = float(drawdown.min()) if not drawdown.empty else 0
    win_rate = (pc > 0).mean() if not pc.empty else 0

    if n_trades > 0:
        win_trades = sum(1 for r in rets_arr if r > 0)
        loss_trades = sum(1 for r in rets_arr if r <= 0)
        streak = 0
        max_losing_streak = 0
        for r in rets_arr:
            if r <= 0:
                streak += 1
                max_losing_streak = max(max_losing_streak, streak)
            else:
                streak = 0
        max_winning_streak = 0
        streak = 0
        for r in rets_arr:
            if r > 0:
                streak += 1
                max_winning_streak = max(max_winning_streak, streak)
            else:
                streak = 0

    result.total_return = total_return
    result.annual_return = annual_return
    result.max_drawdown = max_dd
    result.sharpe_ratio = sharpe
    result.win_rate = win_rate
    result.layer1_pass_rate = layer1_passes / max(layer1_checks, 1)
    result.layer2_pass_rate = layer2_passes / max(layer2_checks, 1)
    result.layer3_avg_picks = top_n
    result.empty_position_ratio = empty_days / max(len(curve) - 1, 1)

    return result, closed_trades


def main():
    import argparse
    parser = argparse.ArgumentParser(description="单票逐笔回测")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--n-stocks", type=int, default=0, help="个股数量 (0=全部)")
    args = parser.parse_args()

    cache_dir = Path("data/raw/_cache/backtest")
    n_stocks = args.n_stocks if args.n_stocks > 0 else 4318
    if args.quick:
        n_stocks = 50

    store = load_all_from_cache(cache_dir, n_stocks=n_stocks)

    # 预计算 L3
    l3_path = cache_dir / "l3_scores_all.parquet"
    if l3_path.exists():
        l3_scores = pd.read_parquet(l3_path)
        logger.info(f"复用 L3 缓存: {len(l3_scores)} 条")
    else:
        l3_scores = precompute_all_stocks(store)
        l3_scores.to_parquet(l3_path, index=False)

    # 只保留 store 中实际存在的股票
    available = set(store.stock_names)
    l3_scores = l3_scores[l3_scores["symbol"].isin(available)].copy()
    logger.info(f"L3 过滤后: {len(l3_scores)} 条 ({len(available)} 只股票)")

    start, end = "2019-01-01", "2026-04-30"

    result, trades = run_trade_backtest(
        store=store,
        l3_scores=l3_scores,
        start=start, end=end, top_n=10,
    )

    n_closed = len(trades)
    rets = [t["return"] for t in trades if t["return"] is not None]

    print("\n" + "=" * 60)
    print("  单票逐笔回测报告")
    print("=" * 60)
    print(f"  累计收益:         {result.total_return:>+9.2%}")
    print(f"  年化收益:         {result.annual_return:>+9.2%}")
    print(f"  最大回撤:         {result.max_drawdown:>-9.2%}")
    print(f"  夏普比率:         {result.sharpe_ratio:>9.3f}")
    print(f"  日胜率:           {result.win_rate:>9.1%}")
    print(f"  L1通过率:         {result.layer1_pass_rate:>9.1%}")
    print(f"  L2通过率:         {result.layer2_pass_rate:>9.1%}")
    print(f"  空仓占比:         {result.empty_position_ratio:>9.1%}")
    print()
    print(f"  ─── 逐笔交易统计 ───")
    print(f"  总交易笔数:       {n_closed:>9}")
    if n_closed > 0:
        avg_ret = np.mean(rets)
        med_ret = np.median(rets)
        std_ret = np.std(rets)
        win_trades = sum(1 for r in rets if r > 0)
        loss_trades = sum(1 for r in rets if r <= 0)
        avg_win = np.mean([r for r in rets if r > 0]) if win_trades > 0 else 0
        avg_loss = np.mean([r for r in rets if r <= 0]) if loss_trades > 0 else 0
        profit_factor = abs(avg_win * win_trades / (avg_loss * loss_trades)) if avg_loss * loss_trades != 0 else float("inf")
        win_rate_trades = win_trades / n_closed

        streak = 0
        max_losing_streak = 0
        for r in rets:
            if r <= 0:
                streak += 1
                max_losing_streak = max(max_losing_streak, streak)
            else:
                streak = 0
        streak = 0
        max_winning_streak = 0
        for r in rets:
            if r > 0:
                streak += 1
                max_winning_streak = max(max_winning_streak, streak)
            else:
                streak = 0

        print(f"  盈利笔数:         {win_trades:>9} ({win_rate_trades:>7.1%})")
        print(f"  亏损笔数:         {loss_trades:>9} ({1-win_rate_trades:>7.1%})")
        print(f"  平均单笔收益:     {avg_ret:>+9.2%}")
        print(f"  单笔收益中位数:   {med_ret:>+9.2%}")
        print(f"  单笔收益标准差:   {std_ret:>9.2%}")
        print(f"  平均盈利:         {avg_win:>+9.2%}")
        print(f"  平均亏损:         {avg_loss:>+9.2%}")
        print(f"  盈亏比:           {abs(avg_win/avg_loss):>9.2f}" if avg_loss != 0 else "  盈亏比:           ∞")
        print(f"  获利因子:         {profit_factor:>9.2f}")
        print(f"  最大连赢:         {max_winning_streak:>9}")
        print(f"  最大连亏:         {max_losing_streak:>9}")

    print(f"\n  分年度逐笔收益:")
    trades_df = pd.DataFrame(trades)
    if not trades_df.empty:
        trades_df["year"] = trades_df["entry_date"].str[:4]
        yearly = trades_df.groupby("year").agg(
            count=("return", "count"),
            win_rate=("return", lambda x: (x > 0).mean()),
            avg_ret=("return", "mean"),
        )
        for y, row in yearly.iterrows():
            print(f"    {y}:  {int(row['count']):>5}笔  "
                  f"胜率{row['win_rate']:>6.1%}  "
                  f"平均{row['avg_ret']:>+7.2%}")

    print("=" * 60)


if __name__ == "__main__":
    main()
