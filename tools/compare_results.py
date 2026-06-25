"""对比旧配置(95%) vs V3.6(130%) 回测结果"""
import json
from collections import defaultdict, Counter

with open('data/backtest_result.json') as f:
    old = json.load(f)
with open('data/backtest_result_v3.json') as f:
    new = json.load(f)

old_sells = [t for t in old['trades'] if t.get('action') == 'SELL']
new_sells = [t for t in new['trades'] if t.get('action') == 'SELL']
old_daily = old['daily_values']
new_daily = new['daily_values']

def year_nav(daily):
    nav = {}
    for d in daily:
        y = d['date'][:4]
        if y not in nav:
            nav[y] = {'first': d['total_value']}
        nav[y]['last'] = d['total_value']
    return nav

old_nav = year_nav(old_daily)
new_nav = year_nav(new_daily)

def year_stats(sells):
    ys = defaultdict(lambda: {'trades':0,'wins':0,'pnl':0})
    for t in sells:
        y = t.get('date','')[:4]
        pnl = t.get('pnl',0)
        ys[y]['trades'] += 1
        ys[y]['pnl'] += pnl
        if pnl > 0: ys[y]['wins'] += 1
    return ys

old_ys = year_stats(old_sells)
new_ys = year_stats(new_sells)

def max_dd(daily):
    peak = 1_000_000
    mdd = 0
    mdd_date = ''
    for d in daily:
        val = d['total_value']
        if val > peak: peak = val
        dd = (val - peak) / peak
        if dd < mdd:
            mdd = dd; mdd_date = d['date']
    return mdd, mdd_date

old_mdd, old_mdd_date = max_dd(old_daily)
new_mdd, new_mdd_date = max_dd(new_daily)

print('| 指标 | 旧配置(95%) | V3.6(130%) | 变化 |')
print('|------|-----------|-----------|------|')
print(f'| 总收益 | {old["total_return"]*100:.2f}% | {new["total_return"]*100:.2f}% | {new["total_return"]/old["total_return"]:.2f}x |')
print(f'| 最终资金 | {old["final_capital"]:,.0f} | {new["final_capital"]:,.0f} | +{new["final_capital"]-old["final_capital"]:,.0f} |')
print(f'| 交易笔数 | {len(old_sells)} | {len(new_sells)} | {"+" if len(new_sells)>len(old_sells) else ""}{len(new_sells)-len(old_sells)} |')
old_wr = len([t for t in old_sells if t['pnl']>0])/len(old_sells)*100
new_wr = len([t for t in new_sells if t['pnl']>0])/len(new_sells)*100
print(f'| 胜率 | {old_wr:.1f}% | {new_wr:.1f}% | {new_wr-old_wr:+.1f}% |')
print(f'| 最大回撤 | {old_mdd*100:.2f}% ({old_mdd_date}) | {new_mdd*100:.2f}% ({new_mdd_date}) | 收窄{abs(old_mdd)-abs(new_mdd):.2f}% |')
print(f'| 是否跑完全程 | {"否(2024-10止)" if old_daily[-1]["date"]<"2025" else "是"} | 是 | — |')

print()
print('年度对比:')
print('| 年份 | 旧收益 | V3.6收益 | 旧交易 | V3.6交易 | 旧胜率 | V3.6胜率 |')
print('|------|-------|---------|--------|---------|-------|---------|')
all_years = sorted(set(list(old_ys.keys()) + list(new_ys.keys())))
for y in all_years:
    o = old_ys.get(y, {'trades':0,'wins':0,'pnl':0})
    n = new_ys.get(y, {'trades':0,'wins':0,'pnl':0})
    o_ret = (old_nav[y]['last']-old_nav[y]['first'])/old_nav[y]['first']*100 if y in old_nav else 0
    n_ret = (new_nav[y]['last']-new_nav[y]['first'])/new_nav[y]['first']*100 if y in new_nav else 0
    o_wr = f'{o["wins"]/o["trades"]*100:.1f}%' if o['trades'] > 0 else '-'
    n_wr = f'{n["wins"]/n["trades"]*100:.1f}%' if n['trades'] > 0 else '-'
    print(f'| {y} | {o_ret:+.2f}% | {n_ret:+.2f}% | {o["trades"]} | {n["trades"]} | {o_wr} | {n_wr} |')

print()
print('出场原因对比:')
reasons_order = ['初始止损', '动态止盈', '目标止盈',
                 '初始止损(减半仓)', '动态止盈(减半仓)',
                 '跌破MA20', '超时退出', '组合风控强制平仓']
old_reasons = Counter(t.get('reason','?') for t in old_sells)
new_reasons = Counter(t.get('reason','?') for t in new_sells)
print('| 出场原因 | 旧配置 | V3.6 |')
print('|---------|--------|------|')
for r in reasons_order:
    oc = old_reasons.get(r, 0)
    nc = new_reasons.get(r, 0)
    if oc > 0 or nc > 0:
        print(f'| {r} | {oc}笔 | {nc}笔 |')
