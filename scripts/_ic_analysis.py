"""Spearman IC 单因子检验 — 逐月截面"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from tqdm import tqdm

from data.store import DataStore
from factors.multi_factor import compute_raw_factors, apply_hard_filters
from core.logger import get_logger

logger = get_logger("ic_analysis")
CACHE_DIR = Path("data/raw/_cache/backtest")
FACTOR_NAMES = [
    "trend_macd_5dim", "momentum_12_1",
    "reversal_20", "oversold_60", "weekly_deviation",
    "bottom_divergence", "volume_reversal", "bottom_fractal",
    "low_volatility", "turnover_stability", "close_position",
]


def load_store(n_stocks=200):
    from backtest.data_loader import load_all_from_cache
    return load_all_from_cache(CACHE_DIR, n_stocks=n_stocks)


def calc_forward_return(daily: pd.DataFrame, month_end, months=1):
    """计算月后收益（%）"""
    next_start = month_end + pd.DateOffset(days=1)
    next_end = month_end + pd.DateOffset(months=months)
    future = daily.loc[next_start:next_end]
    if len(future) < 5:
        return np.nan
    ret = future["close"].iloc[-1] / daily.loc[:month_end, "close"].iloc[-1] - 1
    return ret * 100


def main():
    store = load_store(n_stocks=200)
    symbols = store.stock_names
    logger.info(f"加载 {len(symbols)} 只股票")

    # 收集所有月末
    all_ends = set()
    for sym in symbols[:50]:
        df = store.get_daily(sym)
        if df is not None:
            all_ends.update(df.resample("ME").groups.keys())
    all_ends = sorted(all_ends)
    all_ends = [e for e in all_ends if pd.Timestamp("2020-01-01") <= e <= pd.Timestamp("2024-12-31")]
    logger.info(f"月末时点: {len(all_ends)} 个 ({all_ends[0].date()} ~ {all_ends[-1].date()})")

    # 逐月计算 IC
    records = []
    for month_end in tqdm(all_ends, desc="IC逐月"):
        factors_list = []
        rets_list = []
        for sym in symbols:
            daily = store.get_daily(sym)
            if daily is None or len(daily) < 252:
                continue
            daily_cut = daily.loc[:month_end]
            if len(daily_cut) < 60:
                continue

            weekly = store.get_weekly(sym)
            monthly = store.get_monthly(sym)
            weekly_cut = weekly.loc[:month_end] if weekly is not None else None
            monthly_cut = monthly.loc[:month_end] if monthly is not None else None

            f = compute_raw_factors(sym, daily_cut, weekly_cut, monthly_cut)
            if f is None:
                continue

            fwd_ret = calc_forward_return(daily, month_end)
            if np.isnan(fwd_ret):
                continue

            factors_list.append(f)
            rets_list.append(fwd_ret)

        if len(factors_list) < 30:
            continue

        df = pd.DataFrame(factors_list)
        # apply_hard_filters 会剔除行，需要对齐 forward returns
        orig_idx = df.index.tolist()
        df = apply_hard_filters(df)
        if df.empty:
            continue

        rets_arr = np.array([rets_list[i] for i in df.index if i < len(rets_list)])
        factor_arr = {name: df[name].values for name in FACTOR_NAMES if name in df.columns}

        for fname, fvals in factor_arr.items():
            mask = ~(np.isnan(fvals) | np.isnan(rets_arr[:len(fvals)]))
            if mask.sum() < 20:
                continue
            ic, pval = spearmanr(fvals[mask], rets_arr[:len(fvals)][mask])
            records.append({
                "month": month_end.strftime("%Y-%m"),
                "factor": fname,
                "ic": ic,
                "pval": pval,
                "n": int(mask.sum()),
            })

    result = pd.DataFrame(records)
    if result.empty:
        print("无有效 IC 数据")
        return

    # 汇总
    summary = result.groupby("factor").agg(
        mean_ic=("ic", "mean"),
        std_ic=("ic", "std"),
        icir=("ic", lambda x: x.mean() / max(x.std(), 0.001)),
        pct_positive=("ic", lambda x: (x > 0).mean()),
        pct_significant=("pval", lambda x: (x < 0.05).mean()),
        avg_n=("n", "mean"),
        months=("ic", "count"),
    ).sort_values("mean_ic", ascending=False)

    print("\n" + "=" * 70)
    print("  Spearman IC 检验结果 (2020-2024)")
    print("=" * 70)
    print(f"{'因子':<20} {'Mean IC':>8} {'Std IC':>8} {'ICIR':>8} {'IC>0%':>8} {'p<5%':>8} {'月数':>6}")
    print("-" * 70)
    for _, r in summary.iterrows():
        print(f"{r.name:<20} {r.mean_ic:>+8.4f} {r.std_ic:>8.4f} {r.icir:>+8.2f} "
              f"{r.pct_positive:>7.1%} {r.pct_significant:>7.1%} {r.months:>6.0f}")

    print("\n  —— 参考标准 ——")
    print("  |mean IC|>0.02 = 有预测力")
    print("  ICIR>0.5 = 稳定")
    print("  IC>0%>60% = 方向一致")

    # 按月热力图
    print("\n\n  逐月 IC 热力图:")
    pivot = result.pivot_table(index="month", columns="factor", values="ic")
    print(pivot.to_string(float_format=lambda x: f"{x:+.2f}"))


if __name__ == "__main__":
    main()
