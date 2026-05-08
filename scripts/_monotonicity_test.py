#!/usr/bin/env python
"""
Monotonicity Test for Multi-Factor Model
=========================================
Tests whether the combined score from the multi-factor model has a monotonic
relationship with forward returns. Stocks are grouped into 5 quintiles each
month by combined score, and we check if higher score groups have higher
forward returns.

Key metrics:
  - Spread: Q5 (top quintile) forward return minus Q1 (bottom quintile)
  - Monotonicity: Spearman rank correlation between quintile rank and return

Period: 2020-01 to 2024-12
Universe: 200 stocks from parquet cache
"""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr, ConstantInputWarning

PROJECT_ROOT = Path("D:/projects/quant-system")
sys.path.insert(0, str(PROJECT_ROOT))

from data.store import DataStore
from factors.multi_factor import (
    compute_raw_factors,
    DEFAULT_WEIGHTS,
)

# ── Configuration ──
CACHE_DIR = PROJECT_ROOT / "data/raw/_cache/backtest"
N_STOCKS = 200
FORWARD_DAYS = 21       # ~1 calendar month in trading days
START_DATE = "2020-01-01"
END_DATE = "2024-12-31"
MIN_STOCKS = 30         # skip months with fewer stocks than this


def forward_return(
    daily: pd.DataFrame, date: pd.Timestamp, window: int
) -> float | None:
    """Forward return from `date` over `window` trading days.

    Uses pandas get_indexer(method='pad') for O(log n) lookup so it works
    even when `date` is not a trading day.
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
    """Raw weighted combined score using DEFAULT_WEIGHTS."""
    return sum(factors.get(name, 0.0) * w for name, w in DEFAULT_WEIGHTS.items())


def safe_spearmanr(ranks: list, rets: list) -> tuple[float, float]:
    """Compute Spearman correlation, catching ConstantInputWarning."""
    if len(ranks) < 4:
        return np.nan, np.nan
    with warnings.catch_warnings():
        warnings.simplefilter("error", category=ConstantInputWarning)
        try:
            rho, pval = spearmanr(ranks, rets)
            return float(rho), float(pval)
        except (ConstantInputWarning, RuntimeWarning, UserWarning, ValueError):
            return np.nan, np.nan


def main():
    print("=" * 80, flush=True)
    print("  Multi-Factor Model Monotonicity Test", flush=True)
    print(f"  Period:         {START_DATE} to {END_DATE}", flush=True)
    print(f"  Universe:       {N_STOCKS} stocks", flush=True)
    print(f"  Forward Window: {FORWARD_DAYS} trading days (~1 month)", flush=True)
    print(f"  Min Stocks/Month: {MIN_STOCKS}", flush=True)
    print("=" * 80, flush=True)
    print()

    # ═══════════════════════════════════════════════════════════════════
    #  1. Load data from cache
    # ═══════════════════════════════════════════════════════════════════
    print("[1/4] Loading data from cache ...", flush=True)
    store = DataStore.from_parquet_cache(CACHE_DIR, n_stocks=N_STOCKS)
    symbols = store.stock_names
    print(f"  OK: {len(symbols)} stocks, {len(store)} daily series loaded", flush=True)

    # Pre-filter stocks with enough history
    stock_cache: dict[str, dict] = {}
    for sym in symbols:
        daily = store.get_daily(sym)
        if daily is None or len(daily) < 252:
            continue
        stock_cache[sym] = {"daily": daily}
    print(f"  OK: {len(stock_cache)} stocks pass minimum history (>=252 days)", flush=True)
    print()

    # ═══════════════════════════════════════════════════════════════════
    #  2. Build month-end dates
    # ═══════════════════════════════════════════════════════════════════
    print("[2/4] Building month-end dates ...", flush=True)
    month_ends = pd.date_range(START_DATE, END_DATE, freq="ME")
    print(f"  {len(month_ends)} month-ends: {month_ends[0].date()} to {month_ends[-1].date()}", flush=True)
    print()

    # ═══════════════════════════════════════════════════════════════════
    #  3. Per-month computation
    # ═══════════════════════════════════════════════════════════════════
    print("[3/4] Computing factors & forward returns per month ...", flush=True)
    print()

    monthly_data = []  # list of dicts, one per valid month

    for me in month_ends:
        month_str = me.strftime("%Y-%m")

        records: list[tuple[str, dict, float]] = []
        skip_count = 0

        for sym, cached in stock_cache.items():
            daily = cached["daily"]

            # Truncate to data available up to this month-end (no look-ahead)
            cutoff = daily.loc[:me]
            if len(cutoff) < 60:
                skip_count += 1
                continue

            # Compute raw factors
            try:
                weekly = store.get_weekly(sym)
                monthly_df = store.get_monthly(sym)
                wc = (
                    weekly.loc[:me]
                    if weekly is not None and not weekly.empty
                    else None
                )
                mc = (
                    monthly_df.loc[:me]
                    if monthly_df is not None and not monthly_df.empty
                    else None
                )
                fac = compute_raw_factors(sym, cutoff, wc, mc)
            except Exception:
                fac = None

            if fac is None:
                skip_count += 1
                continue

            # Forward return
            fret = forward_return(daily, me, FORWARD_DAYS)
            if fret is None:
                skip_count += 1
                continue

            # Attach combined score
            fac["combined_score"] = compute_combined_score(fac)
            records.append((sym, fac, fret))

        n_stocks = len(records)
        if n_stocks < MIN_STOCKS:
            print(
                f"  {month_str:>8s}  {n_stocks:4d} stocks  "
                f"(SKIP -- below {MIN_STOCKS} threshold)",
                flush=True,
            )
            continue

        # Build DataFrames
        symbols_list = [r[0] for r in records]
        factors_df = pd.DataFrame([r[1] for r in records], index=symbols_list)
        forward_rets = pd.Series({r[0]: r[2] for r in records})

        # ── Quintile grouping by combined_score ──
        scores = factors_df["combined_score"].dropna()
        if len(scores) < MIN_STOCKS or scores.nunique() < 5:
            print(
                f"  {month_str:>8s}  {n_stocks:4d} stocks  "
                f"(SKIP -- insufficient score variation for quintiles)",
                flush=True,
            )
            continue

        try:
            quintiles = pd.qcut(
                scores, 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"]
            )
        except Exception as exc:
            print(
                f"  {month_str:>8s}  {n_stocks:4d} stocks  "
                f"(SKIP -- qcut error: {exc})",
                flush=True,
            )
            continue

        # Equal-weighted forward return per quintile
        quintile_returns: dict[str, float] = {}
        for q_label in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            mask = quintiles == q_label
            members = scores.index[mask]
            if len(members) == 0:
                quintile_returns[q_label] = np.nan
            else:
                quintile_returns[q_label] = float(forward_rets[members].mean())

        # Spread: top minus bottom
        spread = quintile_returns.get("Q5", np.nan) - quintile_returns.get(
            "Q1", np.nan
        )

        # Spearman: quintile rank (1..5) vs forward return
        rank_map = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "Q5": 5}
        ranks, ret_vals = [], []
        for q_label in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            rv = quintile_returns.get(q_label)
            if rv is not None and not np.isnan(rv):
                ranks.append(rank_map[q_label])
                ret_vals.append(rv)

        spearman_rho, spearman_p = safe_spearmanr(ranks, ret_vals)

        month_entry = {
            "month": month_str,
            "n_stocks": n_stocks,
            "quintiles": quintile_returns,
            "spread": spread,
            "spearman_rho": spearman_rho,
            "spearman_p": spearman_p,
        }
        monthly_data.append(month_entry)

        q1 = quintile_returns.get("Q1", 0)
        q5 = quintile_returns.get("Q5", 0)
        print(
            f"  {month_str:>8s}  {n_stocks:4d} stocks  "
            f"Q1={q1 * 100:+7.2f}%  Q5={q5 * 100:+7.2f}%  "
            f"Spread={spread * 100:+7.2f}%  "
            f"Spearman={spearman_rho:+7.3f}"
            + ("" if np.isnan(spearman_p) else f"  (p={spearman_p:.4f})"),
            flush=True,
        )

    print()
    print(f"  Valid months: {len(monthly_data)} / {len(month_ends)}", flush=True)
    print()

    # ═══════════════════════════════════════════════════════════════════
    #  4. Aggregate & report
    # ═══════════════════════════════════════════════════════════════════
    if len(monthly_data) == 0:
        print("No valid months to analyze.", flush=True)
        return

    print("[4/4] Aggregating results ...", flush=True)
    print()

    # Spread stats
    spreads = np.array(
        [m["spread"] for m in monthly_data if not np.isnan(m["spread"])]
    )
    avg_spread = float(np.mean(spreads)) * 100
    med_spread = float(np.median(spreads)) * 100
    std_spread = float(np.std(spreads)) * 100
    pos_spread_ratio = float(np.mean(spreads > 0)) * 100

    # Spearman stats
    spearman_rhos = np.array(
        [
            m["spearman_rho"]
            for m in monthly_data
            if not np.isnan(m["spearman_rho"])
        ]
    )
    avg_spearman = float(np.mean(spearman_rhos))
    pos_spearman_ratio = float(np.mean(spearman_rhos > 0)) * 100
    n_pos_spearman = int(np.sum(spearman_rhos > 0))
    n_significant = int(
        np.sum(
            [
                1
                for m in monthly_data
                if not np.isnan(m.get("spearman_p", np.nan))
                and m["spearman_p"] < 0.1
            ]
        )
    )

    # Average quintile returns
    q_accum: dict[str, list[float]] = {
        "Q1": [], "Q2": [], "Q3": [], "Q4": [], "Q5": []
    }
    for m in monthly_data:
        for q_label in q_accum:
            v = m["quintiles"].get(q_label)
            if v is not None and not np.isnan(v):
                q_accum[q_label].append(v)

    avg_q = {
        q: float(np.mean(vals)) * 100
        for q, vals in q_accum.items()
        if len(vals) > 0
    }

    # ────────────────────────────────
    #  Print summary
    # ────────────────────────────────
    print("=" * 80, flush=True)
    print("  RESULTS SUMMARY", flush=True)
    print("=" * 80, flush=True)
    print()

    # --- Quintile return curve ---
    print("  Average Forward Return by Quintile:", flush=True)
    print(f"    {'Q1 (lowest score)':>20s}: {avg_q.get('Q1', 0):+8.2f}%", flush=True)
    print(f"    {'Q2':>20s}: {avg_q.get('Q2', 0):+8.2f}%", flush=True)
    print(f"    {'Q3':>20s}: {avg_q.get('Q3', 0):+8.2f}%", flush=True)
    print(f"    {'Q4':>20s}: {avg_q.get('Q4', 0):+8.2f}%", flush=True)
    print(f"    {'Q5 (highest score)':>20s}: {avg_q.get('Q5', 0):+8.2f}%", flush=True)

    # Check monotonicity
    q_order = ["Q1", "Q2", "Q3", "Q4", "Q5"]
    avg_vals = [avg_q.get(q, 0) for q in q_order]
    is_monotonic = all(avg_vals[i] <= avg_vals[i + 1] for i in range(4))
    violations = sum(1 for i in range(4) if avg_vals[i] > avg_vals[i + 1])
    print()
    if is_monotonic:
        print("  Result: Average quintile returns are STRICTLY MONOTONIC "
              "(non-decreasing from Q1 to Q5)", flush=True)
    else:
        print("  Result: Average quintile returns are NOT strictly monotonic", flush=True)
        print(f"  {violations} monotonic violation(s) found", flush=True)
    print()

    # --- Spread ---
    print("  Spread (Q5 - Q1):", flush=True)
    print(f"    Mean spread:   {avg_spread:+8.2f}%", flush=True)
    print(f"    Median spread: {med_spread:+8.2f}%", flush=True)
    print(f"    Std deviation: {std_spread:8.2f}%", flush=True)
    print(f"    % Positive:    {pos_spread_ratio:6.1f}%", flush=True)
    print()

    # --- Spearman ---
    print("  Monotonicity (Spearman rho):", flush=True)
    print(f"    Mean rho:        {avg_spearman:+8.4f}", flush=True)
    print(f"    % Positive rho:  {pos_spearman_ratio:6.1f}%", flush=True)
    print(f"    Positive months: {n_pos_spearman:3d} / {len(spearman_rhos)}", flush=True)
    print(f"    Months p<0.10:   {n_significant:3d} / {len(monthly_data)}", flush=True)
    print()

    # --- Monthly detail table ---
    print("  Monthly Detail:", flush=True)
    header = (
        f"  {'Month':>8s}  {'N':>5s}  "
        f"{'Q1':>8s}  {'Q2':>8s}  {'Q3':>8s}  {'Q4':>8s}  {'Q5':>8s}  "
        f"{'Spread':>8s}  {'Spearman':>8s}"
    )
    print(header, flush=True)
    print("  " + "-" * (len(header) - 2), flush=True)
    for m in monthly_data:
        q = m["quintiles"]
        q1_str = f"{q.get('Q1', 0) * 100:+7.2f}%"
        q2_str = f"{q.get('Q2', 0) * 100:+7.2f}%"
        q3_str = f"{q.get('Q3', 0) * 100:+7.2f}%"
        q4_str = f"{q.get('Q4', 0) * 100:+7.2f}%"
        q5_str = f"{q.get('Q5', 0) * 100:+7.2f}%"
        spread_str = f"{m['spread'] * 100:+7.2f}%"
        rho = m["spearman_rho"]
        rho_str = f"{rho:+7.3f}" if not np.isnan(rho) else "    NaN"

        print(
            f"  {m['month']:>8s}  {m['n_stocks']:5d}  "
            f"{q1_str:>8s}  {q2_str:>8s}  {q3_str:>8s}  "
            f"{q4_str:>8s}  {q5_str:>8s}  {spread_str:>8s}  {rho_str:>8s}",
            flush=True,
        )
    print()

    # ────────────────────────────────
    #  Final verdict
    # ────────────────────────────────
    print("=" * 80, flush=True)
    print("  VERDICT", flush=True)
    print("=" * 80, flush=True)
    print()

    if avg_spread > 0 and avg_spearman > 0 and pos_spread_ratio > 50:
        strength = (
            "strong"
            if avg_spearman > 0.7 and avg_spread > 1.0
            else "moderate"
            if avg_spearman > 0.4 or avg_spread > 0.5
            else "weak"
        )
        print(
            f"  The combined score exhibits {strength} positive monotonicity "
            f"with forward returns.",
            flush=True,
        )
    elif avg_spread < 0 and pos_spread_ratio < 50:
        print(
            "  The combined score shows INVERSE monotonicity -- "
            "lower-scored stocks tend to outperform.",
            flush=True,
        )
    else:
        print(
            "  The combined score does not show clear monotonicity "
            "with forward returns.",
            flush=True,
        )
    print()
    print(
        f"  Summary: Mean spread={avg_spread:+.2f}%  "
        f"Mean Spearman rho={avg_spearman:+.4f}  "
        f"% positive spread={pos_spread_ratio:.0f}%  "
        f"Valid months={len(monthly_data)}",
        flush=True,
    )
    print()
    print("=" * 80, flush=True)
    print("  DONE", flush=True)
    print("=" * 80, flush=True)


if __name__ == "__main__":
    main()
