#!/usr/bin/env python3
"""逐年对比纯SW1 vs ETF融合

日志说明：本脚本使用 print() 输出表格（保留表格格式），不使用 logger 系统。
core/logger.py 的路由表中"scripts.yearly_compare → yearly_compare.log"
为预留条目，当前无实际效果。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics

YEARS = [
    ("2019", "2019-01-01", "2020-01-01"),
    ("2020", "2020-01-01", "2021-01-01"),
    ("2021", "2021-01-01", "2022-01-01"),
    ("2022", "2022-01-01", "2023-01-01"),
    ("2023", "2023-01-01", "2024-01-01"),
    ("2024", "2024-01-01", "2025-01-01"),
    ("2025", "2025-01-01", "2026-01-01"),
    ("2026H1", "2026-01-01", "2026-05-14"),
]

print(f"{'年份':>6} {'纯SW1年化':>12} {'ETF融合年化':>12} {'Δ年化':>10} {'纯SW1-SR':>10} {'ETF-SR':>10} {'纯SW1回撤':>10} {'ETF回撤':>10} {'Δ回撤':>8} {'纯SW1资金':>12} {'ETF资金':>12}")
print("-" * 120)

for label, start, end in YEARS:
    row = {"year": label}
    for fusion_name, fusion_flag in [("纯SW1", False), ("ETF融合", True)]:
        t0 = time.time()
        engine = BacktestEngine()
        result = engine.run(start_date=start, end_date=end,
                            etf_fusion=fusion_flag, use_precomputed=True)
        m = calculate_metrics(result.get("daily_values", []),
                              result.get("trades", []), 1_000_000)
        row[f"ann_{fusion_flag}"] = m.get("annual_return", 0)
        row[f"sr_{fusion_flag}"] = m.get("sharpe", 0)
        row[f"dd_{fusion_flag}"] = m.get("max_drawdown", 0)
        row[f"cap_{fusion_flag}"] = result.get("final_capital", 0)
        t = time.time() - t0
        print(f"  {label} {fusion_name}: 年化={row[f'ann_{fusion_flag}']:.2%} SR={row[f'sr_{fusion_flag}']:.2f} 耗时={t:.0f}s", flush=True)

    delta_ann = row["ann_True"] - row["ann_False"]
    delta_dd = row["dd_True"] - row["dd_False"]
    print(f"{row['year']:>6} {row['ann_False']:>12.2%} {row['ann_True']:>12.2%} {delta_ann:>+10.2%} "
          f"{row['sr_False']:>10.2f} {row['sr_True']:>10.2f} "
          f"{row['dd_False']:>10.2%} {row['dd_True']:>10.2%} {delta_dd:>+8.2%} "
          f"{row['cap_False']:>12.0f} {row['cap_True']:>12.0f}")
