"""回测结果深度分析"""

from sniper.data_router import DataRouter
from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from collections import Counter, defaultdict

router = DataRouter()
engine = BacktestEngine(router)
result = engine.run("2024-01-01", "2024-12-31")

trades = result["trades"]
daily_values = result["daily_values"]
logs = result["logs"]

print(f"{'='*60}")
print(f"回测总览")
print(f"{'='*60}")
print(f"初始资金: 1,000,000")
print(f"最终资金: {result['final_capital']:.2f}")
print(f"总收益率: {result['total_return']*100:.2f}%")
print(f"总交易笔数: {len(trades)}")

# 分析成交记录
buys = [t for t in trades if t.get("action") == "BUY"]
sells = [t for t in trades if t.get("action") == "SELL"]

print(f"\n{'='*60}")
print(f"交易统计")
print(f"{'='*60}")
print(f"买入: {len(buys)} 笔")
print(f"卖出: {len(sells)} 笔")

# 出场原因分析
exit_reasons = Counter()
for t in sells:
    reason = t.get("reason", "未知")
    exit_reasons[reason] += 1

print(f"\n出场原因分布:")
for reason, count in exit_reasons.most_common():
    print(f"  {reason}: {count} 笔")

# 按月份分析
monthly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
for t in trades:
    if t.get("action") == "SELL":
        month = t.get("date", "")[:7]
        pnl = t.get("pnl", 0)
        monthly[month]["pnl"] += pnl
        monthly[month]["trades"] += 1
        if pnl > 0:
            monthly[month]["wins"] += 1

print(f"\n月度表现:")
print(f"{'月份':<10} {'交易':>5} {'胜率':>8} {'总PnL':>12}")
print(f"{'-'*40}")
total_pnl = 0
for month in sorted(monthly.keys()):
    m = monthly[month]
    wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
    total_pnl += m["pnl"]
    print(f"{month:<10} {m['trades']:>5} {wr:>7.1f}% {m['pnl']:>10.0f}")

print(f"\n累计净PnL: {total_pnl:.0f}")

# 按板块分析
sector_pnl = defaultdict(float)
sector_trades = defaultdict(int)
for t in sells:
    sector = t.get("sector", "未知")
    sector_pnl[sector] += t.get("pnl", 0)
    sector_trades[sector] += 1

print(f"\n板块表现(Top 10):")
print(f"{'板块':<15} {'交易':>5} {'总PnL':>12}")
print(f"{'-'*35}")
for sector, pnl in sorted(sector_pnl.items(), key=lambda x: abs(x[1]), reverse=True)[:10]:
    print(f"{sector:<15} {sector_trades[sector]:>5} {pnl:>10.0f}")

# 盈亏比分析
win_trades = [t for t in sells if t.get("pnl", 0) > 0]
loss_trades = [t for t in sells if t.get("pnl", 0) <= 0]
if win_trades and loss_trades:
    avg_win = sum(t["pnl"] for t in win_trades) / len(win_trades)
    avg_loss = abs(sum(t["pnl"] for t in loss_trades) / len(loss_trades))
    print(f"\n盈亏分析:")
    print(f"  盈利交易: {len(win_trades)} 笔 ({(len(win_trades)/len(sells)*100):.1f}%)")
    print(f"  亏损交易: {len(loss_trades)} 笔 ({(len(loss_trades)/len(sells)*100):.1f}%)")
    print(f"  平均盈利: {avg_win:.0f}")
    print(f"  平均亏损: {avg_loss:.0f}")
    print(f"  盈亏比: {avg_win/avg_loss:.2f}")

# 持仓天数分析
hold_days = [t.get("hold_days", 0) for t in sells if t.get("hold_days", 0) > 0]
if hold_days:
    print(f"\n持仓天数:")
    print(f"  最短: {min(hold_days)} 天")
    print(f"  最长: {max(hold_days)} 天")
    print(f"  平均: {sum(hold_days)/len(hold_days):.1f} 天")

# 胜率 vs 盈亏比分析
if win_trades and loss_trades:
    wr = len(win_trades) / len(sells)
    rr = avg_win / avg_loss if avg_loss > 0 else 0
    print(f"\n{'='*60}")
    print(f"策略质量评估")
    print(f"{'='*60}")
    print(f"胜率 (W): {wr*100:.1f}%")
    print(f"盈亏比 (R): {rr:.2f}")
    print(f"期望值 E = W*R - (1-W): {wr*rr - (1-wr):.3f}")
    print(f"(E > 0 表示策略有正期望)")

# 大额亏损交易分析
print(f"\n{'='*60}")
print(f"最大亏损交易 (Top 10)")
print(f"{'='*60}")
losses_sorted = sorted(loss_trades, key=lambda t: t.get("pnl", 0))
for t in losses_sorted[:10]:
    print(f"  {t.get('symbol','?')} PnL={t.get('pnl',0):.0f} 原因={t.get('reason','?')} 日期={t.get('date','?')}")

# 大额盈利交易
print(f"\n最大盈利交易 (Top 10)")
wins_sorted = sorted(win_trades, key=lambda t: t.get("pnl", 0), reverse=True)
for t in wins_sorted[:10]:
    print(f"  {t.get('symbol','?')} PnL={t.get('pnl',0):.0f} 原因={t.get('reason','?')} 日期={t.get('date','?')}")
