#!/usr/bin/env python3
"""ETF融合分层分析 — 按年份和L0区间拆解融合效果

用法:
  python scripts/dissect_fusion.py
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

logger = get_logger("scripts.dissect_fusion")


def run_year(year: str, etf_fusion: bool) -> dict:
    """运行单年回测"""
    end = f"{int(year)+1}-01-01"
    engine = BacktestEngine()
    result = engine.run(start_date=f"{year}-01-01", end_date=end,
                        etf_fusion=etf_fusion, use_precomputed=True)
    trades = result.get("trades", [])
    daily = result.get("daily_values", [])

    # 提取L0
    l0_logs = result.get("l0_scores", {})
    metrics = calculate_metrics(daily, trades, 1_000_000)

    return {
        "year": year,
        "fusion": etf_fusion,
        "metrics": metrics,
        "trades": len(trades),
        "final_capital": result.get("final_capital", 0),
    }


def main():
    years = ["2019", "2020", "2021", "2022", "2023", "2024", "2025"]
    results = []

    for year in years:
        t0 = time.time()
        for fusion in [False, True]:
            r = run_year(year, fusion)
            results.append(r)
            label = "ETF融合" if fusion else "纯SW1"
            ann = r["metrics"].get("annual_return", 0)
            sharpe = r["metrics"].get("sharpe", 0)
            dd = r["metrics"].get("max_drawdown", 0)
            print(f"  {year} {label}: 年化={ann:.2%} Sharpe={sharpe:.2f} 回撤={dd:.2%} 交易={r['trades']}")
        t1 = time.time()
        print(f"  [{year}] 耗时: {t1-t0:.0f}s")

    # 汇总对比
    print(f"\n{'='*70}")
    print(f"{'年份':<8} {'纯SW1年化':<14} {'ETF融合年化':<14} {'Δ':<12} {'纯SW1-SR':<12} {'ETF-SR':<12}")
    print(f"{'='*70}")
    for r_a in [r for r in results if not r["fusion"]]:
        r_b = next(r for r in results if r["fusion"] and r["year"] == r_a["year"])
        delta = r_b["metrics"]["annual_return"] - r_a["metrics"]["annual_return"]
        print(f"{r_a['year']:<8} {r_a['metrics']['annual_return']:<14.2%} "
              f"{r_b['metrics']['annual_return']:<14.2%} {delta:<+12.2%} "
              f"{r_a['metrics']['sharpe']:<12.2f} {r_b['metrics']['sharpe']:<12.2f}")


if __name__ == "__main__":
    main()
