"""全量回测 V3.7 — 使用预计算因子，验证 L1分层 + L0<64主动减仓。

用法: python scripts/run_backtest_v37.py

对比 V3.6 基线: 130.06% 收益, 384 笔卖出, -5.31% 最大回撤
"""

import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

logger = get_logger("scripts.run_backtest_v37")


def main():
    logger.info("开始全量回测 2019-01-01 ~ 2026-05-13")
    logger.info("模式: use_precomputed=True, self_evolve=False")

    t0 = time.time()
    engine = BacktestEngine()
    result = engine.run(cache=False, self_evolve=False, use_precomputed=True)
    elapsed = time.time() - t0

    m = calculate_metrics(
        result.get("daily_values", []),
        result.get("trades", []),
        1_000_000,
    )

    logger.info(f"回测完成, 耗时 {elapsed/60:.1f} 分钟")
    print("\n" + "=" * 50)
    print("  回测结果摘要")
    print("=" * 50)
    print(f"  总收益率:     {m.get('total_return', 0)*100:.2f}%")
    print(f"  年化收益率:   {m.get('annual_return', 0)*100:.2f}%")
    print(f"  最大回撤:     {m.get('max_drawdown', 0)*100:.2f}%")
    print(f"  夏普比率:     {m.get('sharpe_ratio', 0):.2f}")
    print(f"  交易笔数:     {len(result.get('trades', []))}")
    print(f"  日均值:       {len(result.get('daily_values', []))} 天")
    print("=" * 50)

    trades = result.get("trades", [])
    sells = [t for t in trades if t.get("action") == "SELL"]
    print(f"  (其中 SELL: {len(sells)} 笔)")

    summary = {
        "total_return": m.get("total_return", 0),
        "annual_return": m.get("annual_return", 0),
        "max_drawdown": m.get("max_drawdown", 0),
        "sharpe_ratio": m.get("sharpe_ratio", 0),
        "total_trades": len(trades),
        "sell_count": len(sells),
        "daily_days": len(result.get("daily_values", [])),
        "elapsed_min": round(elapsed / 60, 1),
    }

    out = Path("outputs/backtest_result_v3.7.json")
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()
