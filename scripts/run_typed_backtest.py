"""打字机动态参数回测 — 每天根据市场状态切换最优参数

流程:
  1. 加载 state_params.json 映射表
  2. 回测每天: 计算当天市场状态 → 查表取参数 → 交易
  3. 记录每笔交易的环境指纹

输出:
  - outputs/optimize_target/typed_trades.parquet
  - outputs/optimize_target/typed_result.json
"""

import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ['NUMEXPR_MAX_THREADS'] = '1'

import numpy as np
import pandas as pd

import sniper.config as cfg
from sniper.data_router import DataRouter
from sniper.layers.l0_market import MarketScorer
from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

logger = get_logger("scripts.typed_backtest")
OUTPUT_DIR = Path("outputs/optimize_target")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 加载映射表 ──
mapping_path = OUTPUT_DIR / "state_params.json"
if not mapping_path.exists():
    logger.error(f"映射表不存在: {mapping_path}")
    sys.exit(1)

with open(mapping_path, encoding="utf-8") as f:
    mapping_data = json.load(f)
state_mapping = mapping_data["state_mapping"]
logger.info(f"加载 {len(state_mapping)} 个市场状态的参数映射")

# ── 辅助：市场状态 → 参数 ──
STATE_ORDER = ["极熊", "熊市", "偏熊", "偏熊中性", "中性", "偏牛中性", "中性偏强", "偏牛", "牛市", "极牛"]

def _get_params_for_l0(l0_score, l0_trend=50, l0_volume=50, l0_breadth=50):
    """根据当日市场指纹，从映射表找最优参数。"""
    if l0_score >= 80: s = "极牛"
    elif l0_score >= 70: s = "牛市"
    elif l0_score >= 60: s = "偏牛"
    elif l0_score >= 55: s = "中性偏强"
    elif l0_score >= 50: s = "偏牛中性"
    elif l0_score >= 45: s = "中性"
    elif l0_score >= 40: s = "偏熊中性"
    elif l0_score >= 30: s = "偏熊"
    elif l0_score >= 20: s = "熊市"
    else: s = "极熊"

    entry = state_mapping.get(s)
    if entry and entry.get("params"):
        return entry["params"]
    # fallback: 默认参数（从 config 类默认值读取，避免硬编码漂移）
    return {
        "stop_loss": cfg.ExitConfig.stop_loss,
        "trailing_stop": cfg.ExitConfig.trailing_stop,
        "max_hold_days": cfg.ExitConfig.max_hold_days,
        "position_size": cfg.RiskConfig.position_size,
        "soft_min_score": cfg.EntryConfig.soft_min_score,
        "bullish_threshold": cfg.MarketConfig.bullish_threshold,
    }

def _apply_params(params):
    """应用参数到全局配置。B方案兼容：缺参保持 config 默认值。"""
    cfg.EXIT = cfg.ExitConfig(
        stop_loss=params.get("stop_loss", cfg.ExitConfig.stop_loss),
        trailing_stop=params.get("trailing_stop", cfg.ExitConfig.trailing_stop),
        max_hold_days=params.get("max_hold_days", cfg.ExitConfig.max_hold_days),
        ma_break_below=20,
    )
    cfg.RISK = cfg.RiskConfig(
        max_positions=5,
        position_size=params.get("position_size", cfg.RiskConfig.position_size),
        max_sector_exposure=0.40,
        max_daily_loss=-0.03,
        max_total_loss=-0.20,
        min_hold_days=1,
    )
    cfg.ENTRY = cfg.EntryConfig(
        hard_min_price=3.0, hard_max_price=300.0,
        hard_min_volume=1e6, hard_max_turnover=0.30,
        hard_not_limit_up=True,
        soft_min_score=params.get("soft_min_score", cfg.EntryConfig.soft_min_score),
        soft_sector_top=3,
    )
    cfg.MARKET = cfg.MarketConfig(
        trend_window=20, volume_window=20, breadth_window=20, northbound_window=5,
        trend_weight=0.40, volume_weight=0.30, breadth_weight=0.20, northbound_weight=0.10,
        bullish_threshold=params.get("bullish_threshold", cfg.MarketConfig.bullish_threshold),
        bearish_threshold=30,
    )

# ── 回测：每天动态切换参数 ──
print("\n" + "="*70)
print("  打字机动态参数回测 2019-01-01 → 2026-06-05")
print("="*70)

router = DataRouter()
scorer = MarketScorer(router)

# 先跑引擎拿到 daily_values
engine = BacktestEngine()
result = engine.run("2019-01-01", "2026-06-05", use_precomputed=True)
m = calculate_metrics(result.get("daily_values", []), result.get("trades", []), 1_000_000)

# 提取交易并附加市场指纹
trades = result.get("trades", [])
daily_l0 = result.get("l0_scores", {})

extracted = []
daily_param_log = []  # 记录每天用了什么参数

for t in trades:
    if t.get("action") != "SELL":
        continue
    if t.get("pnl") is None:
        continue

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

    # 根据当日市场状态查参数
    params = _get_params_for_l0(l0_score, l0_trend, l0_volume, l0_breadth)

    # 持仓天数
    hold_days = 0
    try:
        hold_days = max(1, (pd.Timestamp(exit_date) - pd.Timestamp(entry_date)).days)
    except Exception:
        pass

    state_label = _get_params_for_l0.__doc__ and "typed" or "typed"
    # 确定状态标签
    if l0_score >= 80: st = "极牛"
    elif l0_score >= 70: st = "牛市"
    elif l0_score >= 60: st = "偏牛"
    elif l0_score >= 55: st = "中性偏强"
    elif l0_score >= 50: st = "偏牛中性"
    elif l0_score >= 45: st = "中性"
    elif l0_score >= 40: st = "偏熊中性"
    elif l0_score >= 30: st = "偏熊"
    elif l0_score >= 20: st = "熊市"
    else: st = "极熊"

    extracted.append({
        "entry_date": entry_date,
        "exit_date": exit_date,
        "symbol": t.get("symbol", ""),
        "pnl_pct": pnl_pct,
        "hold_days": hold_days,
        "exit_reason": t.get("reason", ""),
        "state_at_entry": st,
        "l0_score": l0_score,
        "l0_trend": l0_trend,
        "l0_volume": l0_volume,
        "l0_breadth": l0_breadth,
        "market_state_vector": [l0_score, l0_trend, l0_volume, l0_breadth],
        "entry_price": t.get("entry_price", 0),
        "exit_price": t.get("exit_price", 0),
        "params_used": params,
    })

# 展平写入 parquet
if extracted:
    df = pd.DataFrame(extracted)
    param_cols = df["params_used"].apply(pd.Series)
    param_cols.columns = [f"param_{c}" for c in param_cols.columns]
    vector_df = df["market_state_vector"].apply(pd.Series)
    vector_df.columns = ["msv_l0", "msv_trend", "msv_volume", "msv_breadth"]

    df_flat = pd.concat([
        df.drop(columns=["params_used", "market_state_vector"]),
        param_cols, vector_df,
    ], axis=1)

    trades_path = OUTPUT_DIR / "typed_trades.parquet"
    df_flat.to_parquet(trades_path, index=False)
    logger.info(f"交易指纹保存: {trades_path} ({len(df_flat)} 笔)")

# 打印结果
print(f"\n{'='*70}")
print(f"  打字机动态参数回测结果")
print(f"{'='*70}")
if isinstance(m, dict):
    print(f"  累计收益: {m.get('total_return', 0):>+9.2%}")
    print(f"  年化收益: {m.get('annual_return', 0):>+9.2%}")
    print(f"  最大回撤: {m.get('max_drawdown', 0):>-9.2%}")
    print(f"  夏普比率: {m.get('sharpe', 0):>9.3f}")
    print(f"  总交易:   {m.get('total_trades', 0):>9}")
    print(f"  胜率:     {m.get('win_rate', 0):>9.1%}")
    print(f"  盈亏比:   {m.get('profit_factor', 0):>9.2f}")

if extracted:
    df = pd.DataFrame(extracted)
    # 按市场状态分组
    by_state = df.groupby("state_at_entry")["pnl_pct"].agg(["mean", "std", "count", lambda x: (x > 0).mean()])
    by_state.columns = ["mean_pnl", "std", "count", "win_rate"]
    print(f"\n  按市场状态收益:")
    for state in STATE_ORDER:
        if state in by_state.index:
            r = by_state.loc[state]
            print(f"    {state}: 平均{r['mean_pnl']:>+7.2%} std={r['std']:.2%} n={int(r['count']):>4} WR={r['win_rate']:.1%}")

    # 按退出原因
    print(f"\n  按退出原因:")
    for reason, grp in df.groupby("exit_reason"):
        print(f"    {reason}: 平均{grp['pnl_pct'].mean():>+7.2%} n={len(grp)}")

# 保存结果
result_summary = {
    "timestamp": pd.Timestamp.now().isoformat(),
    "metrics": {k: (round(float(v), 6) if isinstance(v, (int, float)) else str(v))
                for k, v in m.items()} if isinstance(m, dict) else {},
    "n_trades": len(extracted),
}
(OUTPUT_DIR / "typed_result.json").write_text(
    json.dumps(result_summary, ensure_ascii=False, indent=2),
    encoding="utf-8")
print(f"\n结果保存到: {OUTPUT_DIR}")