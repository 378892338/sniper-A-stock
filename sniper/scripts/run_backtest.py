"""运行回测 — python -m sniper.scripts.run_backtest"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics, print_metrics_table
from sniper.config import BACKTEST
from core.logger import get_logger

logger = get_logger("sniper.scripts.run_backtest")


def main():
    logger.info("=" * 50)
    logger.info("狙击手系统 — 回测启动")
    logger.info(f"区间: {BACKTEST.start_date} ~ {BACKTEST.end_date}")
    logger.info(f"本金: {BACKTEST.initial_capital:.0f}")
    logger.info("=" * 50)

    engine = BacktestEngine()
    result = engine.run()

    if not result:
        logger.error("回测无结果")
        return

    # 评估
    metrics = calculate_metrics(
        result.get("daily_values", []),
        result.get("trades", []),
        BACKTEST.initial_capital,
    )
    print_metrics_table(metrics)

    logger.info(f"交易次数: {metrics.get('total_trades', 0)}")
    logger.info(f"年化收益: {metrics.get('annual_return', 0):.2%}")
    logger.info(f"Sharpe: {metrics.get('sharpe', 0):.2f}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
