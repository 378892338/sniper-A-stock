"""因子验证脚本 — Step 1: 单因子 IC + Step 2: 多空分组

用法:
  python scripts/validate_factors.py --year 2023 --n-stocks 500
  python scripts/validate_factors.py --year 2023 --n-stocks 500 --step 2
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from tqdm import tqdm

from data.store import DataStore
from factors.multi_factor import (
    compute_raw_factors, process_cross_section, aggregate_scores,
    apply_hard_filters, DEFAULT_WEIGHTS,
    _FACTOR_COLUMNS,
)
from core.logger import get_logger

logger = get_logger("validate.factors")

_FACTOR_LABELS = {
    "trend_macd_5dim": "5维MACD",
    "momentum_12_1": "12-1月动量",
    "reversal_20": "20日反转",
    "oversold_60": "60日超卖",
    "weekly_deviation": "周线偏离",
    "bottom_divergence": "底背驰",
    "volume_reversal": "量能反转",
    "bottom_fractal": "底分型",
    "low_volatility": "低波动",
    "turnover_stability": "换手稳定",
    "close_position": "收盘位置",
}


def step1_factor_ic(store, year=2023):
    """Step 1: 单因子 Spearman IC 检验"""
    symbols = store.stock_names
    logger.info(f"IC检验: {len(symbols)} 只股票, {year}年")

    all_month_ends = set()
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is not None and len(daily) >= 252:
            ends = [e for e in daily.resample("ME").groups.keys()
                    if str(e.year) == str(year)]
            all_month_ends.update(ends)
    all_month_ends = sorted(all_month_ends)

    if not all_month_ends:
        logger.error(f"{year}年无数据")
        return

    logger.info(f"月份: {len(all_month_ends)} 个")

    ic_records = {f: [] for f in _FACTOR_COLUMNS}

    for month_end in tqdm(all_month_ends, desc="IC检验"):
        month_str = month_end.strftime("%Y-%m")

        # 获取下月末价格计算 forward return
        next_month = month_end + pd.DateOffset(months=1)
        # 找到下个月最后一个有数据的交易日
        next_end_candidates = [
            e for e in all_month_ends if e > month_end
        ]
        if not next_end_candidates:
            # 用 month_end + 21 个交易日
            next_end = month_end + pd.DateOffset(days=31)
        else:
            next_end = next_end_candidates[0]

        # 批量计算因子
        all_factors = []
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

            # 计算 forward return (从 month_end 到 next_end)
            fwd_daily = daily.loc[month_end:next_end]
            if len(fwd_daily) >= 2:
                f["_fwd_ret"] = float(fwd_daily["close"].iloc[-1] / fwd_daily["close"].iloc[0] - 1)
            else:
                continue

            all_factors.append(f)

        if len(all_factors) < 30:
            continue

        # 截面标准化
        factors_df = pd.DataFrame(all_factors)
        factors_df = apply_hard_filters(factors_df)
        fwd_rets = factors_df["_fwd_ret"].values
        symbols_batch = factors_df["symbol"].tolist()

        factor_data = factors_df[["symbol"] + [c for c in _FACTOR_COLUMNS if c in factors_df.columns]]
        processed = process_cross_section(factor_data.to_dict("records"))
        if processed.empty:
            continue

        # 计算每个因子的 Spearman IC
        for f_col in _FACTOR_COLUMNS:
            if f_col not in processed.columns:
                continue
            factor_vals = processed[f_col].dropna().values
            if len(factor_vals) < 20:
                continue
            # 只取有 fwd_ret 的
            valid_idx = processed.index.intersection(factors_df[factors_df["_fwd_ret"].notna()]["symbol"])
            if len(valid_idx) < 20:
                continue
            f_vals = processed.loc[valid_idx, f_col].values
            r_vals = factors_df.set_index("symbol").loc[valid_idx, "_fwd_ret"].values
            try:
                ic, pval = spearmanr(f_vals, r_vals)
                if not np.isnan(ic):
                    ic_records[f_col].append(ic)
            except Exception:
                continue

    # ── 报告 ──
    print("\n" + "=" * 72)
    print(f"  Step 1: 单因子 Spearman IC 检验 ({year}年, {len(all_month_ends)}个月)")
    print("=" * 72)
    print(f"  {'因子':<16} {'月均IC':>8} {'IC>0占比':>9} {'ICIR':>7} {'评价':>8}")
    print("  " + "-" * 56)

    passed = 0
    total = 0
    for f_col in _FACTOR_COLUMNS:
        ics = ic_records.get(f_col, [])
        if not ics:
            continue
        total += 1
        ics = np.array(ics)
        mean_ic = np.mean(ics)
        pos_ratio = (ics > 0).mean()
        icir = mean_ic / np.std(ics) if np.std(ics) > 0 else 0
        label = _FACTOR_LABELS.get(f_col, f_col)

        if mean_ic > 0.02 and pos_ratio > 0.55:
            grade = "PASS"
            passed += 1
        elif mean_ic > 0:
            grade = "WEAK"
        else:
            grade = "REVERSED"

        print(f"  {label:<16} {mean_ic:>+8.4f} {pos_ratio:>8.1%} {icir:>+7.3f} {grade:>8}")

    print("  " + "-" * 56)
    print(f"  通过率: {passed}/{total}")
    print("=" * 72)

    return ic_records


def step2_quantile_groups(store, year=2023):
    """Step 2: 多空分组单调性检验"""
    symbols = store.stock_names
    logger.info(f"多空分组: {len(symbols)} 只股票, {year}年")

    all_month_ends = set()
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is not None and len(daily) >= 252:
            ends = [e for e in daily.resample("ME").groups.keys()
                    if str(e.year) == str(year)]
            all_month_ends.update(ends)
    all_month_ends = sorted(all_month_ends)

    group_returns = {f"Q{i+1}": [] for i in range(5)}
    group_counts = {f"Q{i+1}": [] for i in range(5)}

    for month_end in tqdm(all_month_ends, desc="分组检验"):
        next_end_candidates = [e for e in all_month_ends if e > month_end]
        if not next_end_candidates:
            continue
        next_end = next_end_candidates[0]

        all_factors = []
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
            fwd_daily = daily.loc[month_end:next_end]
            if len(fwd_daily) >= 2:
                f["_fwd_ret"] = float(fwd_daily["close"].iloc[-1] / fwd_daily["close"].iloc[0] - 1)
            else:
                continue
            all_factors.append(f)

        if len(all_factors) < 50:
            continue

        factors_df = pd.DataFrame(all_factors)
        factors_df = apply_hard_filters(factors_df)
        if len(factors_df) < 50:
            continue

        factor_data = factors_df[["symbol"] + [c for c in _FACTOR_COLUMNS if c in factors_df.columns]]
        processed = process_cross_section(factor_data.to_dict("records"))
        if processed.empty:
            continue

        scores = aggregate_scores(processed)
        scores["_fwd_ret"] = factors_df.set_index("symbol")["_fwd_ret"]

        # 分5组
        valid = scores.dropna(subset=["_fwd_ret"])
        if len(valid) < 50:
            continue
        valid = valid.sort_values("score")
        n = len(valid)
        boundaries = [0, n//5, 2*n//5, 3*n//5, 4*n//5, n]

        for q in range(5):
            group = valid.iloc[boundaries[q]:boundaries[q+1]]
            if len(group) > 0:
                group_returns[f"Q{q+1}"].append(float(group["_fwd_ret"].mean()))
                group_counts[f"Q{q+1}"].append(len(group))

    # ── 报告 ──
    print("\n" + "=" * 72)
    print(f"  Step 2: 多空分组单调性检验 ({year}年)")
    print("=" * 72)
    print(f"  {'分组':<8} {'月均收益':>10} {'累计收益':>10} {'平均数量':>8}")
    print("  " + "-" * 44)

    cum_rets = {}
    for q in range(5):
        q_name = f"Q{q+1}"
        rets = np.array(group_returns[q_name])
        if len(rets) == 0:
            continue
        mean_ret = np.mean(rets)
        cum_ret = float((1 + pd.Series(rets)).prod() - 1)
        cum_rets[q_name] = cum_ret
        avg_n = int(np.mean(group_counts[q_name])) if group_counts[q_name] else 0
        label = "(Top)" if q == 4 else ("(Bot)" if q == 0 else "")
        print(f"  {q_name} {label:<6} {mean_ret:>+10.4%} {cum_ret:>+10.2%} {avg_n:>8}")

    # 多空收益差
    if "Q5" in cum_rets and "Q1" in cum_rets:
        long_short = (1 + cum_rets["Q5"]) / (1 + cum_rets["Q1"]) - 1
        print("  " + "-" * 44)
        print(f"  Top - Bottom 累计多空差: {long_short:>+.2%}")
        if long_short > 0:
            print("  [OK] Direction correct")
        else:
            print("  [FAIL] Direction reversed!")

    # 单调性检查
    rets_seq = [np.mean(group_returns[f"Q{q+1}"]) for q in range(5)
                if len(group_returns[f"Q{q+1}"]) > 0]
    if len(rets_seq) == 5:
        if all(rets_seq[i] <= rets_seq[i+1] for i in range(len(rets_seq)-1)):
            print("  [OK] Monotonic increasing")
        else:
            print("  [FAIL] Not monotonic, adjust weights")

    print("=" * 72)
    return group_returns


def main():
    parser = argparse.ArgumentParser(description="因子验证")
    parser.add_argument("--year", type=int, default=2023, help="验证年份")
    parser.add_argument("--n-stocks", type=int, default=500, help="股票数量")
    parser.add_argument("--step", type=int, default=1, choices=[1, 2], help="验证步骤")
    parser.add_argument("--cache-dir", type=str, default="data/raw/_cache/backtest")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    store = DataStore.from_parquet_cache(cache_dir, n_stocks=args.n_stocks)
    logger.info(f"DataStore: {len(store)} 条数据")

    if args.step == 1:
        step1_factor_ic(store, year=args.year)
    elif args.step == 2:
        step2_quantile_groups(store, year=args.year)


if __name__ == "__main__":
    main()
