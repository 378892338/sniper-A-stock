#!/usr/bin/env python
"""
因子单调性测试 (Factor Monotonicity Test)
========================================
检查各因子是否与未来1个月收益呈单调关系。
每月末将股票按因子值分为5组 (Q1最低 ~ Q5最高)，
检验 Q5 > Q4 > Q3 > Q2 > Q1 的严格单调比例。

范围: 2020-01 ~ 2024-12, 200只股票
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path("D:/projects/quant-system")
sys.path.insert(0, str(PROJECT_ROOT))

from data.store import DataStore
from factors.multi_factor import compute_raw_factors, DEFAULT_WEIGHTS, _FACTOR_COLUMNS

# ── 配置 ──
CACHE_DIR = PROJECT_ROOT / "data/raw/_cache/backtest"
N_STOCKS = 200
FORWARD_DAYS = 21        # ~1个自然月
START = "2020-01"
END = "2024-12"

ALL_FACTORS = _FACTOR_COLUMNS + ["combined_score"]

FACTOR_DISPLAY = {
    "trend_macd_5dim": "五维MACD趋势",
    "momentum_12_1": "动量(12月-1月)",
    "reversal_20": "20日反转",
    "oversold_60": "60日超卖",
    "weekly_deviation": "周线偏离度",
    "bottom_divergence": "底背驰",
    "volume_reversal": "量能反转",
    "bottom_fractal": "底分型",
    "low_volatility": "低波动率",
    "turnover_stability": "换手率稳定",
    "close_position": "收盘位置",
    "combined_score": "综合得分",
}


def flush():
    sys.stdout.flush()


# ── 工具函数 ──

def forward_return_fast(
    daily: pd.DataFrame, date: pd.Timestamp, window: int
) -> float | None:
    """
    计算自 date (或之前最近交易日) 起 window 个交易日后的累计收益。
    使用 pandas get_indexer(method='pad') 实现 O(log n) 查找。
    返回小数收益率 (e.g. 0.05 = 5%).
    """
    if daily is None or daily.empty:
        return None

    idx = daily.index.get_indexer([date], method="pad")[0]
    if idx < 0 or idx >= len(daily):
        return None

    p0 = daily.iloc[idx]["close"]
    if pd.isna(p0) or p0 <= 0:
        return None

    future = idx + window
    if future >= len(daily):
        return None

    p1 = daily.iloc[future]["close"]
    if pd.isna(p1) or p1 <= 0:
        return None

    return float(p1 / p0 - 1)


def compute_combined_score(factors: dict) -> float:
    """用 DEFAULT_WEIGHTS 计算原始加权综合得分 (未标准化)"""
    return sum(factors.get(name, 0.0) * w for name, w in DEFAULT_WEIGHTS.items())


# ── 主逻辑 ──

def main():
    print("=" * 72, flush=True)
    print("  因子单调性测试", flush=True)
    print(f"  范围: {START} ~ {END}  |  个股: {N_STOCKS}  |  前向窗口: {FORWARD_DAYS}交易日", flush=True)
    print("=" * 72, flush=True)

    t_start = time.time()

    # ── 1. 加载数据 ──
    print("\n[1/4] 加载数据 ...", flush=True)
    store = DataStore.from_parquet_cache(CACHE_DIR, n_stocks=N_STOCKS)
    symbols = store.stock_names
    print(f"  OK: {len(symbols)} 只个股, {len(store)} 条日线", flush=True)

    # 预加载所有股票日线
    print("  预加载日线数据 ...", flush=True)
    stock_cache = {}
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is None or len(daily) < 120:
            continue
        stock_cache[sym] = {"daily": daily}
    print(f"  OK: {len(stock_cache)} 只股票通过初步筛选", flush=True)

    # ── 2. 月末截面 ──
    month_ends = list(pd.date_range(f"{START}-01", periods=60, freq="ME"))
    print(f"\n[2/4] 月末截面: {len(month_ends)} 个", flush=True)
    print(f"  {month_ends[0].strftime('%Y-%m-%d')} ~ {month_ends[-1].strftime('%Y-%m-%d')}", flush=True)

    # ── 3. 逐月计算 ──
    print("\n[3/4] 逐月计算因子 & 前向收益 ...", flush=True)

    monthly_results = []

    for i, me in enumerate(month_ends):
        month_str = me.strftime("%Y-%m")
        records: list[tuple[str, dict, float]] = []
        n_skip = 0

        for sym, cached in stock_cache.items():
            daily = cached["daily"]

            # 月末截断 (用于因子计算)
            cutoff = daily.loc[:me]
            if len(cutoff) < 60:
                n_skip += 1
                continue

            # 因子计算
            try:
                weekly = store.get_weekly(sym)
                monthly = store.get_monthly(sym)
                wc = weekly.loc[:me] if weekly is not None and not weekly.empty else None
                mc = monthly.loc[:me] if monthly is not None and not monthly.empty else None
                fac = compute_raw_factors(sym, cutoff, wc, mc)
            except Exception:
                fac = None

            if fac is None:
                n_skip += 1
                continue

            # 前向收益 (pandas get_indexer, O(log n))
            fret = forward_return_fast(daily, me, FORWARD_DAYS)
            if fret is None:
                n_skip += 1
                continue

            # 综合得分
            fac["combined_score"] = compute_combined_score(fac)

            records.append((sym, fac, fret))

        if len(records) < 20:
            print(f"  {month_str:>8s}  {len(records):>5d}  {n_skip:>5d}  SKIP (样本不足)", flush=True)
            continue

        # ── 分位数分组 ──
        fac_df = pd.DataFrame([r[1] for r in records], index=[r[0] for r in records])
        ret_s = pd.Series({r[0]: r[2] for r in records})

        month_rec = {"month": month_str, "n": len(records)}

        for fname in ALL_FACTORS:
            if fname not in fac_df.columns:
                continue
            vals = fac_df[fname].dropna()
            if len(vals) < 10 or vals.nunique() < 3:
                continue

            try:
                quint = pd.qcut(vals, 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"], duplicates="drop")
            except Exception:
                continue
            if quint.nunique() < 5:
                continue

            ok = True
            for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
                members = vals.index[quint == q]
                if len(members) == 0:
                    ok = False
                    break
                month_rec[f"{fname}_{q}"] = float(ret_s[members].mean())
            if not ok:
                continue

        monthly_results.append(month_rec)

        elapsed = time.time() - t_start
        print(f"  {month_str:>8s}  {len(records):>5d}  {n_skip:>5d}  OK  [{elapsed:6.0f}s]", flush=True)

    n_valid = len(monthly_results)
    print(f"\n  有效月份: {n_valid} / {len(month_ends)}", flush=True)

    # ── 4. 汇总 ──
    print(f"\n[4/4] 汇总分析 ...\n", flush=True)

    # 找出所有出现过的因子
    factor_set = set()
    for rec in monthly_results:
        for k in rec:
            for q in ["_Q1", "_Q2", "_Q3", "_Q4", "_Q5"]:
                if k.endswith(q):
                    factor_set.add(k[:-3])

    factor_list = sorted(factor_set)

    rows = []
    for fname in factor_list:
        # 收集每个有效月份的分位数收益
        q_months: list[list[float]] = []
        for rec in monthly_results:
            qs = []
            ok = True
            for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
                v = rec.get(f"{fname}_{q}")
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    ok = False
                    break
                qs.append(v)
            if ok:
                q_months.append(qs)

        if len(q_months) < 6:
            continue

        arr = np.array(q_months)
        avg_q = np.nanmean(arr, axis=0)

        # 严格单调比例 (Q5>Q4>Q3>Q2>Q1)
        n_strict = sum(1 for qs in q_months if all(qs[i] > qs[i-1] for i in range(1, 5)))
        mono_pct = n_strict / len(q_months) * 100

        # Spread 统计
        spreads = [qs[4] - qs[0] for qs in q_months]
        avg_spread = np.mean(spreads) * 100
        med_spread = np.median(spreads) * 100
        pos_ratio = sum(1 for s in spreads if s > 0) / len(spreads) * 100

        dname = FACTOR_DISPLAY.get(fname, fname)
        rows.append({
            "因子名": fname,
            "显示名": dname,
            "月份数": len(q_months),
            "严格单调%": mono_pct,
            "松散单调%": pos_ratio,
            "平均Spread%": avg_spread,
            "中位Spread%": med_spread,
            "Spread>0%": pos_ratio,
            "Q1%": avg_q[0] * 100,
            "Q2%": avg_q[1] * 100,
            "Q3%": avg_q[2] * 100,
            "Q4%": avg_q[3] * 100,
            "Q5%": avg_q[4] * 100,
        })

    df_out = pd.DataFrame(rows).sort_values("严格单调%", ascending=False)

    # ═══════════════════════════════════════
    # 打印详细结果
    # ═══════════════════════════════════════

    print("=" * 72, flush=True)
    print("  因子单调性测试结果 (按严格单调比例降序)", flush=True)
    print("=" * 72, flush=True)
    print(flush=True)

    header = f"  {'因子':>22s}  {'严格单调':>8s}  {'松散单调':>7s}  {'Spread':>8s}  {'Q1->Q5收益曲线':>35s}"
    print(header, flush=True)
    print("  " + "-" * (len(header) - 2), flush=True)

    for _, r in df_out.iterrows():
        mp = r["严格单调%"]
        sp = r["平均Spread%"]
        if mp > 60 and sp > 0.3:
            tag = "[*]"
        elif mp > 40:
            tag = "[v]"
        elif sp < -0.3 and mp > 20:
            tag = "[!]"
        elif mp > 20:
            tag = "[~]"
        else:
            tag = "[ ]"

        qline = (f"Q1={r['Q1%']:+.2f}%  Q2={r['Q2%']:+.2f}%  "
                 f"Q3={r['Q3%']:+.2f}%  Q4={r['Q4%']:+.2f}%  Q5={r['Q5%']:+.2f}%")

        spread_str = f"{r['平均Spread%']:+.2f}%"
        mono_str = f"{r['严格单调%']:.1f}%"
        loose_str = f"{r['松散单调%']:.0f}%"

        bar_len = int(min(abs(sp), 10))
        bar = "+" * bar_len if sp > 0 else "-" * bar_len if sp < 0 else ""

        print(f"  {tag} {r['显示名']:>20s}  {mono_str:>8s}  {loose_str:>7s}  {spread_str:>8s}  {bar}", flush=True)
        print(f"      {'':>22s}  {qline}", flush=True)
        print(flush=True)

    # ═══════════════════════════════════════
    # 按类别汇总
    # ═══════════════════════════════════════
    print("=" * 72, flush=True)
    print("  三维度平均单调性", flush=True)
    print("=" * 72, flush=True)
    print(flush=True)

    groups = {
        "Trend 趋势": ["trend_macd_5dim", "momentum_12_1"],
        "Alpha 买点": ["reversal_20", "oversold_60", "weekly_deviation",
                        "bottom_divergence", "volume_reversal", "bottom_fractal"],
        "Risk 风控":  ["low_volatility", "turnover_stability", "close_position"],
    }

    for gname, gfactors in groups.items():
        grows = [r for _, r in df_out.iterrows() if r["因子名"] in gfactors]
        if not grows:
            continue
        avg_mono = np.mean([r["严格单调%"] for r in grows])
        avg_spd = np.mean([r["平均Spread%"] for r in grows])
        print(f"  {gname:20s}: 平均严格单调={avg_mono:.1f}%  |  平均Spread={avg_spd:+.2f}%", flush=True)
        for r in grows:
            bar = "+" * int(min(abs(r["平均Spread%"]), 10)) if r["平均Spread%"] > 0 else "-" * int(min(abs(r["平均Spread%"]), 10))
            print(f"    {r['显示名']:>18s}: 严格单调={r['严格单调%']:5.1f}%  "
                  f"Spread={r['平均Spread%']:+7.2f}%  {bar}", flush=True)
        print(flush=True)

    # ═══════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════
    print("=" * 72, flush=True)
    print("  总结", flush=True)
    print("=" * 72, flush=True)
    print(flush=True)

    # 1. 优秀
    excellent = df_out[(df_out["严格单调%"] > 60) & (df_out["平均Spread%"] > 0.3)]
    if not excellent.empty:
        print(f"  [优秀] 严格单调比例>60% + Spread>0.3%:", flush=True)
        for _, r in excellent.iterrows():
            print(f"    [*] {r['显示名']:20s}  严格单调 {r['严格单调%']:.1f}%  "
                  f"Spread {r['平均Spread%']:+.2f}%  "
                  f"Q1={r['Q1%']:.2f}%  Q5={r['Q5%']:.2f}%", flush=True)
        print(flush=True)

    # 2. 较好
    good = df_out[(df_out["严格单调%"] > 40) & (df_out["严格单调%"] <= 60)]
    if not good.empty:
        print(f"  [较好] 严格单调比例40%-60%:", flush=True)
        for _, r in good.iterrows():
            print(f"    [v] {r['显示名']:20s}  严格单调 {r['严格单调%']:.1f}%  "
                  f"Spread {r['平均Spread%']:+.2f}%", flush=True)
        print(flush=True)

    # 3. 高判别力
    high_spread = df_out[df_out["平均Spread%"] > 1.0]
    if not high_spread.empty:
        print(f"  [高判别力] 平均Spread > 1%:", flush=True)
        for _, r in high_spread.iterrows():
            print(f"    [^] {r['显示名']:20s}  Spread {r['平均Spread%']:+.2f}%  "
                  f"严格单调 {r['严格单调%']:.1f}%", flush=True)
        print(flush=True)

    # 4. 反向因子
    inverted = df_out[(df_out["平均Spread%"] < -0.3) & (df_out["严格单调%"] > 20)]
    if not inverted.empty:
        print(f"  [反向] 平均Spread<0 (Q1跑赢Q5):", flush=True)
        for _, r in inverted.iterrows():
            print(f"    [!] {r['显示名']:20s}  Spread {r['平均Spread%']:+.2f}%  "
                  f"Q1={r['Q1%']:.2f}%  Q5={r['Q5%']:.2f}%", flush=True)
        print(flush=True)

    # 5. 综合得分
    combined_row = df_out[df_out["因子名"] == "combined_score"]
    if not combined_row.empty:
        r = combined_row.iloc[0]
        print(f"  [综合得分]", flush=True)
        print(f"    严格单调比例: {r['严格单调%']:.1f}%", flush=True)
        print(f"    松散单调比例: {r['松散单调%']:.1f}%", flush=True)
        print(f"    平均Spread:   {r['平均Spread%']:+.2f}%", flush=True)
        print(f"    中位Spread:   {r['中位Spread%']:+.2f}%", flush=True)
        print(f"    收益曲线:     Q1={r['Q1%']:.2f}%  Q2={r['Q2%']:.2f}%  "
              f"Q3={r['Q3%']:.2f}%  Q4={r['Q4%']:.2f}%  Q5={r['Q5%']:.2f}%", flush=True)
        print(flush=True)

    # 6. 综合排名
    print(f"  [因子有效性排名]", flush=True)
    rank = df_out.sort_values("严格单调%", ascending=False)
    for i, (_, r) in enumerate(rank.iterrows(), 1):
        bar = "+" * int(min(abs(r["平均Spread%"]), 8)) if r["平均Spread%"] > 0 else "-" * int(min(abs(r["平均Spread%"]), 8))
        print(f"    {i:2d}. {r['显示名']:20s}  单调{r['严格单调%']:5.1f}%  "
              f"Spread{r['平均Spread%']:+7.2f}%  {bar}", flush=True)

    elapsed = time.time() - t_start
    print(flush=True)
    print("=" * 72, flush=True)
    print(f"  测试完成  |  耗时: {elapsed:.0f}s", flush=True)
    print("=" * 72, flush=True)


if __name__ == "__main__":
    main()
