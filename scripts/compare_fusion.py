#!/usr/bin/env python3
"""AB回测对比 — 纯SW1 vs ETF融合

用法:
  python scripts/compare_fusion.py                      # 默认对比回测
  python scripts/compare_fusion.py --start 2023-01-01    # 指定区间
  python scripts/compare_fusion.py --bootstrap 1000      # bootstrap统计检验

输出:
  outputs/compare_fusion/
    comparison_report.json    # 结构化对比报告
    control_A_pure_sw1.json   # 对照组详细结果
    experiment_B_fusion.json  # 实验组详细结果
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# 确保能导入sniper
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

logger = get_logger("scripts.compare_fusion")
OUTPUT_DIR = Path("outputs/compare_fusion")


def run_single(etf_fusion: bool, label: str,
               start: str, end: str) -> dict:
    """运行单次回测, 返回完整结果"""
    logger.info(f"[{label}] 开始回测 {start}~{end}")
    engine = BacktestEngine()
    result = engine.run(
        start_date=start,
        end_date=end,
        etf_fusion=etf_fusion,
        use_precomputed=True,
    )
    logger.info(f"[{label}] 完成: NAV={result.get('final_capital', 0):.0f}")
    return result


def extract_metrics(result: dict) -> dict:
    """从回测结果中提取绩效指标"""
    metrics = calculate_metrics(
        daily_values=result.get("daily_values", []),
        trades=result.get("trades", []),
        initial_capital=1_000_000,
    )
    return {
        "final_capital": result.get("final_capital", 0),
        "total_return": result.get("total_return", 0),
        "annual_return": metrics.get("annual_return", 0),
        "sharpe": metrics.get("sharpe", 0),        # 修正: metrics.py用sharpe不是sharpe_ratio
        "max_drawdown": metrics.get("max_drawdown", 0),
        "total_trades": metrics.get("total_trades", 0),
        "win_rate": metrics.get("win_rate", 0),
        "profit_factor": metrics.get("profit_factor", 0),
    }


def run_bootstrap(control_results: list, experiment_results: list,
                  n_iter: int = 1000) -> dict:
    """Bootstrap检验两组收益差异的统计显著性(评审FAIL-15修复)"""
    def _compute_pnl(t: dict) -> float:
        """从trade记录计算盈亏(用cost作为总成本,price为均价)"""
        if t.get("action") == "SELL":
            cost = t.get("cost", 0) or 0
            proceeds = t.get("price", 0) * t.get("shares", 0)
            pnl = proceeds - cost
            return pnl / cost if cost > 0 else 0.0
        return 0.0

    control_pnls = [_compute_pnl(t) for t in control_results
                    if t.get("action") == "SELL" and t.get("cost", 0) > 0]
    exp_pnls = [_compute_pnl(t) for t in experiment_results
                if t.get("action") == "SELL" and t.get("cost", 0) > 0]

    if len(control_pnls) < 10 or len(exp_pnls) < 10:
        return {"error": "交易笔数不足(各需>=10)"}

    np.random.seed(42)
    diffs = []
    for _ in range(n_iter):
        c_sample = np.random.choice(control_pnls, size=len(control_pnls), replace=True)
        e_sample = np.random.choice(exp_pnls, size=len(exp_pnls), replace=True)
        diffs.append(np.mean(e_sample) - np.mean(c_sample))

    diffs = np.array(diffs)
    mean_diff = float(np.mean(diffs))
    ci_low = float(np.percentile(diffs, 2.5))
    ci_high = float(np.percentile(diffs, 97.5))
    p_value = float(np.sum(diffs <= 0) / n_iter * 2)  # 双尾

    return {
        "n_iter": n_iter,
        "mean_diff_pnl": round(mean_diff, 6),
        "ci_95": [round(ci_low, 6), round(ci_high, 6)],
        "p_value": round(p_value, 4),
        "significant": p_value < 0.05,
        "control_trades": len(control_pnls),
        "experiment_trades": len(exp_pnls),
    }


def main():
    parser = argparse.ArgumentParser(description="AB回测对比: 纯SW1 vs ETF融合")
    parser.add_argument("--start", default="2019-01-01", help="回测开始日期")
    parser.add_argument("--end", default="2026-05-13", help="回测结束日期")
    parser.add_argument("--bootstrap", type=int, default=1000,
                        help="Bootstrap迭代次数(0=不执行)")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help="输出目录")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 对照组: 纯SW1
    t0 = time.time()
    control = run_single(etf_fusion=False, label="纯SW1(对照组A)",
                         start=args.start, end=args.end)
    t1 = time.time()
    logger.info(f"对照组耗时: {t1-t0:.1f}s")

    # 实验组: ETF融合
    experiment = run_single(etf_fusion=True, label="ETF融合(实验组B)",
                            start=args.start, end=args.end)
    t2 = time.time()
    logger.info(f"实验组耗时: {t2-t1:.1f}s")

    # 指标对比
    c_metrics = extract_metrics(control)
    e_metrics = extract_metrics(experiment)

    comparison = {
        "回测区间": f"{args.start} ~ {args.end}",
        "对照组A(纯SW1)": c_metrics,
        "实验组B(ETF融合)": e_metrics,
        "增量(Δ)": {
            k: round(e_metrics.get(k, 0) - c_metrics.get(k, 0), 4)
            for k in c_metrics
        },
    }

    # Bootstrap 统计检验
    if args.bootstrap > 0:
        control_trades = control.get("trades", [])
        exp_trades = experiment.get("trades", [])
        bootstrap_result = run_bootstrap(
            control_trades, exp_trades, n_iter=args.bootstrap)
        comparison["bootstrap检验"] = bootstrap_result

    # 输出
    report_path = OUTPUT_DIR / "comparison_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2)

    # 打印摘要
    print(f"\n{'='*60}")
    print(f"  AB回测对比: 纯SW1 vs ETF融合")
    print(f"  区间: {args.start} ~ {args.end}")
    print(f"{'='*60}")
    print(f"  {'指标':<20} {'纯SW1(A)':<12} {'ETF融合(B)':<12} {'Δ':<12}")
    print(f"  {'-'*56}")
    for k in ["annual_return", "sharpe", "max_drawdown",
              "total_trades", "win_rate", "final_capital"]:
        c_val = c_metrics.get(k, 0)
        e_val = e_metrics.get(k, 0)
        delta = e_val - c_val
        if isinstance(c_val, float):
            print(f"  {k:<20} {c_val:<12.4f} {e_val:<12.4f} {delta:<+12.4f}")
        else:
            print(f"  {k:<20} {c_val:<12} {e_val:<12} {delta:<+12}")

    if "bootstrap检验" in comparison:
        b = comparison["bootstrap检验"]
        if "error" in b:
            print(f"\n  Bootstrap: {b['error']}")
        else:
            sig = "(显著)" if b["significant"] else "(不显著)"
            print(f"\n  Bootstrap({b['n_iter']}次): "
                  f"Δ均值={b['mean_diff_pnl']:.6f}, "
                  f"p={b['p_value']:.4f} {sig}")
            print(f"  95%CI: [{b['ci_95'][0]:.6f}, {b['ci_95'][1]:.6f}]")

    print(f"\n  报告已保存: {report_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
