"""实盘运行入口 — python -m scripts.run_live [date]

流程:
  1. load_paper_tape()                    # 加载纸带（启动时一次）
  2. MarketScorer.score_all(today)        # 算今天市场指纹
  3. configure_for_today(...)             # 归因 + 切换参数
  4. BacktestEngine.run(today, today)     # 跑今天的策略
  5. append_to_paper_tape(completed_trade) # 追加平仓交易到纸带
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import sniper.config as CFG


def daily_run(today: str | None = None) -> None:
    """执行一天的实盘流程。

    Args:
        today: 日期 YYYY-MM-DD，默认取当前日期。
    """
    import datetime

    from sniper.config import load_paper_tape, append_to_paper_tape
    from sniper.config import configure_for_today
    from sniper.engine.backtest import BacktestEngine
    from sniper.layers.l0_market import MarketScorer
    from sniper.data_router import DataRouter
    from core.logger import get_logger

    logger = get_logger("scripts.run_live")

    today = today or datetime.datetime.now().strftime("%Y-%m-%d")
    logger.info(f"{'='*50}")
    logger.info(f"实盘运行: {today}")
    logger.info(f"{'='*50}")

    # ① 加载纸带
    load_paper_tape()

    # ② 计算今天市场指纹
    router = DataRouter()
    scorer = MarketScorer(router)
    l0_info = scorer.score_all(today)
    logger.info(
        f"市场指纹: 合成={l0_info['composite']:.1f} "
        f"趋势={l0_info['trend']:.1f} 量能={l0_info['volume']:.1f} "
        f"宽度={l0_info['breadth']:.1f}"
    )

    # ③ 归因 + 切换参数
    configure_for_today(
        l0_score=l0_info["composite"],
        l0_trend=l0_info["trend"],
        l0_volume=l0_info["volume"],
        l0_breadth=l0_info["breadth"],
    )

    # 记录切换后的参数快照（从 _PARAMS_META 动态构建，消除 7 参数硬编码）
    params_snapshot = {}
    for param_k, config_name in CFG._PARAM_TO_CONFIG.items():
        config_obj = getattr(CFG, config_name)
        params_snapshot[param_k] = getattr(config_obj, param_k)

    # ④ 跑今天的策略
    engine = BacktestEngine(router)
    result = engine.run(today, today)

    if not result:
        logger.error("今日回测无结果")
        return

    trades = result.get("trades", [])
    completed = [t for t in trades if t.get("action") == "SELL" and t.get("pnl") is not None]
    logger.info(f"今日交易: {len(trades)} 笔, 平仓: {len(completed)} 笔")

    # ⑤ 追加平仓交易到纸带
    for trade in completed:
        entry_price = trade.get("entry_price", 0)
        exit_price = trade.get("price", 0)
        pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0

        ed = trade.get("entry_date", today)
        xd = trade.get("date", today)
        try:
            ed_dt = datetime.datetime.strptime(ed, "%Y-%m-%d")
            xd_dt = datetime.datetime.strptime(xd, "%Y-%m-%d")
            hold_days = max(1, (xd_dt - ed_dt).days)
        except ValueError:
            hold_days = 1

        tape_entry = {
            "params": dict(params_snapshot),
            "pnl_pct": pnl_pct,
            "hold_days": hold_days,
            "exit_reason": trade.get("reason", ""),
            "symbol": trade.get("symbol", ""),
            "entry_date": ed,
            "exit_date": xd,
            "l0_score": l0_info["composite"],
            "l0_trend": l0_info["trend"],
            "l0_volume": l0_info["volume"],
            "l0_breadth": l0_info["breadth"],
            "market_state_vector": [
                l0_info["composite"],
                l0_info["trend"],
                l0_info["volume"],
                l0_info["breadth"],
            ],
        }
        append_to_paper_tape(tape_entry)
        logger.info(f"纸带追加: {trade['symbol']} PnL={pnl_pct:.2%}")

    # ⑥ 持久化持仓快照（供 14:45 盘中初稿读取）
    _save_position_snapshot(engine, today)

    logger.info(f"实盘完成: {today}")
    logger.info(f"{'='*50}")


def _save_position_snapshot(engine, today: str) -> None:
    """将当日持仓序列化到 parquet，供 14:45 盘中初稿读取。

    保存所有 active positions 的关键字段：
      symbol, shares, entry_price, entry_date, sector, score
    """
    import pandas as pd
    from pathlib import Path

    positions = getattr(engine.risk, "positions", {})
    if not positions:
        return

    rows = []
    for sym, pos in positions.items():
        rows.append({
            "symbol": sym,
            "shares": pos.get("shares", 0),
            "entry_price": pos.get("entry_price", 0),
            "entry_date": str(pos.get("entry_date", ""))[:10],
            "sector": pos.get("sector", ""),
            "score": pos.get("score", 0),
        })

    out = Path("outputs/position_snapshot.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(out, index=False)


def main():
    import argparse

    a = argparse.ArgumentParser(description="狙击手实盘运行入口")
    a.add_argument("--date", type=str, default="", help="运行日期 YYYY-MM-DD，默认当天")
    args = a.parse_args()
    daily_run(args.date or None)


if __name__ == "__main__":
    main()
