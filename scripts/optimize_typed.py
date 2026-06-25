"""打字机归因跑参数优化 + 录全量交易指纹

输出:
  - outputs/optimize_target/optimal_backtest_result.json — 回测统计
  - outputs/optimize_target/optimal_trades.parquet — 每笔交易含市场指纹
  - outputs/optimize_target/optimal_params.json — 各市场状态最优参数
"""

import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ['NUMEXPR_MAX_THREADS'] = '1'

import numpy as np
import pandas as pd

import sniper.config as cfg
from sniper.data_router import DataRouter
from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics, print_metrics_table

from core.logger import get_logger
logger = get_logger("scripts.optimize_typed")

OUTPUT_DIR = Path("outputs/optimize_target")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 加载纸带做归因 ──
cfg.load_paper_tape()
tape = cfg._TRADE_PAPER
logger.info(f"纸带加载: {len(tape)} 笔交易")

# 全局最优
global_optimal = cfg._attribution(tape)
logger.info(f"全局最优参数: {global_optimal}")

# 各市场状态最优
states = {
    "bullish":  (75, 80, 60, 65, "牛"),
    "neutral":  (50, 50, 50, 50, "震荡"),
    "bearish":  (25, 20, 30, 30, "熊"),
}
state_params = {}
for name, (s, t, v, b, label) in states.items():
    neighbors = cfg._find_neighbors([s, t, v, b])
    if neighbors and len(neighbors) >= 10:
        sp = cfg._attribution(neighbors)
        state_params[name] = {"label": label, "fingerprint": [s, t, v, b], "params": sp}
        logger.info(f"  [{label}] params: {sp}")

# 保存参数表
(OUTPUT_DIR / "optimal_params.json").write_text(
    json.dumps({"global": global_optimal, "per_state": state_params,
                "timestamp": pd.Timestamp.now().isoformat()},
               ensure_ascii=False, indent=2),
    encoding="utf-8")
logger.info("参数表已保存")

# ── 用全局最优参数跑回测 ──
print("\n" + "="*70)
print("  应用打字机最优参数跑回测 2019-01-01 → 2026-06-05")
print("="*70)

# 应用参数到全局配置
EXIT = cfg.ExitConfig(
    stop_loss=global_optimal.get("stop_loss", cfg.ExitConfig.stop_loss),
    trailing_stop=global_optimal.get("trailing_stop", cfg.ExitConfig.trailing_stop),
    max_hold_days=global_optimal.get("max_hold_days", cfg.ExitConfig.max_hold_days),
    ma_break_below=20,
)
RISK = cfg.RiskConfig(
    max_positions=5,
    position_size=global_optimal.get("position_size", cfg.RiskConfig.position_size),
    max_sector_exposure=0.40,
    max_daily_loss=-0.03,
    max_total_loss=-0.20,
    min_hold_days=1,
)
ENTRY = cfg.EntryConfig(
    hard_min_price=3.0, hard_max_price=300.0,
    hard_min_volume=1e6, hard_max_turnover=0.30,
    hard_not_limit_up=True,
    soft_min_score=global_optimal.get("soft_min_score", cfg.EntryConfig.soft_min_score),
    soft_sector_top=3,
)
MARKET = cfg.MarketConfig(
    trend_window=20, volume_window=20, breadth_window=20, northbound_window=5,
    trend_weight=0.40, volume_weight=0.30, breadth_weight=0.20, northbound_weight=0.10,
    bullish_threshold=global_optimal.get("bullish_threshold", cfg.MarketConfig.bullish_threshold),
    bearish_threshold=30,
)

cfg.EXIT = EXIT
cfg.RISK = RISK
cfg.ENTRY = ENTRY
cfg.MARKET = MARKET

print(f"\n应用参数:")
print(f"  stop_loss={EXIT.stop_loss}, trailing_stop={EXIT.trailing_stop}")
print(f"  max_hold_days={EXIT.max_hold_days}")
print(f"  position_size={RISK.position_size}, soft_min_score={ENTRY.soft_min_score}")
print(f"  bullish_threshold={MARKET.bullish_threshold}")

# 跑回测
engine = BacktestEngine()
result = engine.run("2019-01-01", "2026-06-05", use_precomputed=True)
m = calculate_metrics(result.get("daily_values", []), result.get("trades", []), 1_000_000)

print("\n" + "="*70)
print("  回测结果")
print("="*70)
if isinstance(m, dict):
    print(f"  累计收益: {m.get('total_return', 0):>+9.2%}")
    print(f"  年化收益: {m.get('annual_return', 0):>+9.2%}")
    print(f"  最大回撤: {m.get('max_drawdown', 0):>-9.2%}")
    print(f"  夏普比率: {m.get('sharpe', 0):>9.3f}")
    print(f"  总交易:   {m.get('total_trades', 0):>9}")
    print(f"  胜率:     {m.get('win_rate', 0):>9.1%}")
    print(f"  盈亏比:   {m.get('profit_factor', 0):>9.2f}")
else:
    print_metrics_table(m)

# ── 录全量交易指纹 ──
trades = result.get("trades", [])
daily_l0 = result.get("l0_scores", {})
logger.info(f"Raw trades: {len(trades)}, daily_l0 dates: {len(daily_l0)}")

extracted = []
for t in trades:
    if t.get("action") != "SELL":
        continue
    if t.get("pnl") is None:
        continue

    # pnl_pct
    pnl_pct = t.get("pnl_pct")
    if pnl_pct is None:
        cost = t.get("cost", 0) or 0
        pnl_abs = t.get("pnl", 0) or 0
        pnl_pct = pnl_abs / cost if cost > 0 else 0.0
    else:
        pnl_pct = pnl_pct or 0.0

    entry_date = t.get("entry_date", "")
    exit_date = t.get("date", entry_date)
    if not entry_date:
        continue

    # 市场指纹
    l0_info = daily_l0.get(entry_date, {})
    l0_score = l0_info.get("composite", 50.0)
    l0_trend = l0_info.get("trend", 50.0)
    l0_volume = l0_info.get("volume", 50.0)
    l0_breadth = l0_info.get("breadth", 50.0)

    # 持仓天数
    hold_days = 0
    try:
        ed = pd.Timestamp(entry_date)
        xd = pd.Timestamp(exit_date)
        hold_days = max(1, (xd - ed).days)
    except Exception:
        pass

    extracted.append({
        "entry_date": entry_date,
        "exit_date": exit_date,
        "symbol": t.get("symbol", ""),
        "pnl_pct": pnl_pct,
        "hold_days": hold_days,
        "exit_reason": t.get("reason", ""),
        "l0_score": l0_score,
        "l0_trend": l0_trend,
        "l0_volume": l0_volume,
        "l0_breadth": l0_breadth,
        "market_state_vector": [l0_score, l0_trend, l0_volume, l0_breadth],
        "params": dict(global_optimal),  # 当前使用的参数快照
        "entry_price": t.get("entry_price", 0),
        "exit_price": t.get("exit_price", 0),
    })

if extracted:
    # 展平 params 写入 parquet
    df = pd.DataFrame(extracted)
    param_cols = df["params"].apply(pd.Series)
    param_cols.columns = [f"param_{c}" for c in param_cols.columns]
    vector_df = df["market_state_vector"].apply(pd.Series)
    vector_df.columns = ["msv_l0", "msv_trend", "msv_volume", "msv_breadth"]

    df_flat = pd.concat([
        df.drop(columns=["params", "market_state_vector"]),
        param_cols, vector_df,
    ], axis=1)

    trades_path = OUTPUT_DIR / "optimal_trades.parquet"
    df_flat.to_parquet(trades_path, index=False)
    logger.info(f"交易指纹已保存: {trades_path} ({len(df_flat)} 笔)")

    # 统计摘要
    print(f"\n{'='*70}")
    print(f"  交易指纹统计 ({len(df_flat)} 笔)")
    print(f"{'='*70}")
    print(f"  平均收益:   {df_flat['pnl_pct'].mean():>+7.2%}")
    print(f"  中位收益:   {df_flat['pnl_pct'].median():>+7.2%}")
    print(f"  胜率:       {(df_flat['pnl_pct']>0).mean():>7.1%}")
    print(f"  平均持仓:   {df_flat['hold_days'].mean():>7.0f}天")
    print(f"  覆盖股票:   {df_flat['symbol'].nunique()} 只")

    # 按市场状态分组收益
    df_flat["state"] = pd.cut(df_flat["l0_score"],
        bins=[0, 30, 50, 100], labels=["熊", "震荡", "牛"])
    state_stats = df_flat.groupby("state")["pnl_pct"].agg(["mean", "std", "count"])
    print(f"\n  按市场状态:")
    for state, row in state_stats.iterrows():
        print(f"    {state}: 平均{row['mean']:>+7.2%} std={row['std']:.2%} n={int(row['count']):>4}")

    # 按退出原因分组
    reason_stats = df_flat.groupby("exit_reason")["pnl_pct"].agg(["mean", "count"])
    print(f"\n  按退出原因:")
    for reason, row in reason_stats.sort_values("mean", ascending=False).iterrows():
        print(f"    {reason}: 平均{row['mean']:>+7.2%} n={int(row['count'])}")
else:
    logger.warning("无有效交易记录")

# 保存回测结果
result_summary = {
    "timestamp": pd.Timestamp.now().isoformat(),
    "params": global_optimal,
    "metrics": {k: (round(float(v), 6) if isinstance(v, (int, float)) else str(v))
                for k, v in m.items()} if isinstance(m, dict) else {},
    "n_trades": len(extracted),
}
(OUTPUT_DIR / "optimal_backtest_result.json").write_text(
    json.dumps(result_summary, ensure_ascii=False, indent=2),
    encoding="utf-8")
print(f"\n结果已保存到 {OUTPUT_DIR}")