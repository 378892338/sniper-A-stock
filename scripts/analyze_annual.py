"""按年分析回测结果 — 每年独立引擎实例，确保数据隔离。

用法: python scripts/analyze_annual.py
"""

import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

logger = get_logger("scripts.analyze_annual")

YEARS = [
    ("2019", "2019-01-01", "2019-12-31"),
    ("2020", "2020-01-01", "2020-12-31"),
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026", "2026-01-01", "2026-05-13"),
]


def main():
    all_rows = []

    print("=" * 90)
    print(f"  V3.7 按年回测分析 (use_precomputed=True, self_evolve=False)")
    print("=" * 90)
    print(f"| {'年份':<6} | {'总收益':>8} | {'年化':>7} | {'最大回撤':>8} | {'夏普':>5} | {'交易':>5} | {'SELL':>5} | {'天数':>5} |")
    print(f"|{'------':-^8}|{'--------':-^10}|{'-------':-^9}|{'--------':-^10}|{'-----':-^7}|{'-----':-^7}|{'-----':-^7}|{'-----':-^7}|")

    for label, start, end in YEARS:
        engine = BacktestEngine()
        result = engine.run(start, end, use_precomputed=True)
        m = calculate_metrics(
            result.get("daily_values", []),
            result.get("trades", []),
            1_000_000,
        )
        trades = result.get("trades", [])
        sells = len([t for t in trades if t.get("action") == "SELL"])
        days = len(result.get("daily_values", []))

        tr = m.get("total_return", 0) * 100
        ar = m.get("annual_return", 0) * 100
        dd = m.get("max_drawdown", 0) * 100
        sharpe = m.get("sharpe", 0)

        all_rows.append({
            "year": label,
            "total_return": round(tr, 2),
            "annual_return": round(ar, 2),
            "max_drawdown": round(dd, 2),
            "sharpe": round(sharpe, 2),
            "total_trades": len(trades),
            "sell_count": sells,
            "days": days,
        })

        print(f"| {label:<6} | {tr:>7.2f}% | {ar:>6.2f}% | {dd:>7.2f}% | {sharpe:>4.2f} | {len(trades):>4} | {sells:>4} | {days:>4} |")

    # 保存
    out = Path("outputs/annual_analysis_v3.7.json")
    out.write_text(json.dumps(all_rows, indent=2), encoding="utf-8")
    print(f"\n已保存: {out}")


if __name__ == "__main__":
    main()
