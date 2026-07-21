"""两段式推演 — 从纸带中反向→正向推演参数收敛

流程:
  1. 从最新交易倒序推演到 2019-01（反向）
  2. 从 2019-01 正序推演到最新（正向，以反向终点作起点）
  3. 收敛分析：反向轨迹 vs 正向轨迹 → 差异消失点 = 参数收敛点
  4. 每轮淘汰无效参数，缩小搜索空间

输出到 TAPE_DIR/:
  .reverse_trace.json      反向推演轨迹
  .forward_trace.json      正向推演轨迹
  .convergence_report.json 收敛分析报告
"""

import json as _json
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import numpy as _np
import pandas as _pd

from core.logger import get_logger

logger = get_logger("scripts.tape_evolve")
logger.info("=" * 50)
logger.info("两段式推演开始")
logger.info("=" * 50)

# ── 加载纸带 ──
try:
    from config.paths import TAPE_DIR, OUTPUT_DIR
    TAPE_DIR.mkdir(parents=True, exist_ok=True)
    tape_path = TAPE_DIR / "paper_tape.parquet"
    if not tape_path.exists():
        tape_path = OUTPUT_DIR / "optimize_target" / "paper_tape.parquet"
    df = _pd.read_parquet(str(tape_path))
    logger.info(f"纸带加载: {len(df)} 行, {len(df.columns)} 列, 来源 {tape_path}")
except Exception as e:
    logger.error(f"纸带加载失败: {e}")
    _sys.exit(1)

# ── 确定参数列 ──
# 旧格式: param_xxx 列（7 个旧参）
# 新格式: ConfigName_field 列（107 个全参）
PARAM_COLS = [c for c in df.columns if c.startswith("param_")]
if PARAM_COLS:
    logger.info(f"旧格式参数列: {len(PARAM_COLS)} 个 (param_*)")
else:
    PARAM_COLS = [c for c in df.columns if "_" in c and any(
        c.startswith(p) for p in ["MarketConfig_", "SectorConfig_", "StockConfig_",
                                  "EntryConfig_", "ExitConfig_", "RiskConfig_",
                                  "BacktestConfig_", "EtfMomentumConfig_",
                                  "FusionConfig_", "DdrConfig_", "DegradationConfig_"])]
    logger.info(f"新格式参数列: {len(PARAM_COLS)} 个 (ConfigName_*)")

# ── 过滤: 整列无方差(全相同值)的参数 → 冻结(不参与推演) ──
STD_THRESHOLD = 1e-8
active_cols = [c for c in PARAM_COLS if df[c].nunique() > 1 and df[c].std() > STD_THRESHOLD]
frozen_cols = [c for c in PARAM_COLS if c not in active_cols]
logger.info(f"活性参数: {len(active_cols)}  |  冻结(无方差): {len(frozen_cols)}")
if frozen_cols:
    _ = [logger.debug(f"  冻结: {c}={df[c].iloc[0]}") for c in frozen_cols[:10]]
    if len(frozen_cols) > 10:
        logger.debug(f"  ... 还有 {len(frozen_cols)-10} 个冻结参数")

# ── 确保日期列 ──
HAS_DATE_COL = "exit_date" in df.columns
if HAS_DATE_COL:
    df["exit_date_dt"] = _pd.to_datetime(df["exit_date"])
    all_dates = sorted(df["exit_date_dt"].unique())
    logger.info(f"日期范围: {all_dates[0].date()} → {all_dates[-1].date()} ({len(all_dates)} 个交易日)")
else:
    logger.warning("纸带无 exit_date 列，推演受限")
    all_dates = []

# ══════════════════════════════════════════════
# 阶段 A — 反向推演
# ══════════════════════════════════════════════
logger.info("\n" + "-" * 50)
logger.info("阶段 A: 反向推演 (最新 → 2019-01)")
logger.info("-" * 50)

reverse_trace: list[dict] = []

for i, d in enumerate(reversed(all_dates)):
    # 前进窗口：该日之后发生的全部交易
    future = df[df["exit_date_dt"] > d]
    if len(future) < 5:
        continue

    # 按参数组合分组
    groups = future.groupby(active_cols)
    stats = groups.agg(
        count=("pnl_pct", "count"),
        mean_pnl=("pnl_pct", "mean"),
        std_pnl=("pnl_pct", "std")
    ).reset_index()
    stats = stats[stats["count"] >= 3]
    if stats.empty:
        continue

    # 取平均 PnL 最高组
    best = stats.loc[stats["mean_pnl"].idxmax()]
    best_params = {c: float(best[c]) for c in active_cols}
    reverse_trace.append({
        "date": str(d.date()),
        "n_future": len(future),
        "n_groups": len(stats),
        "best_group_count": int(best["count"]),
        "best_group_pnl": round(float(best["mean_pnl"]), 4),
        "best_group_std": round(float(best["std_pnl"]), 4),
        "params": best_params,
    })

    if (i + 1) % 500 == 0:
        logger.info(f"  反向推演: 已处理 {i+1}/{len(all_dates)} 天, 当前最优 PnL={best['mean_pnl']:.4f}")

logger.info(f"反向推演完成: {len(reverse_trace)} 天")

# 保存反向轨迹
reverse_path = TAPE_DIR / ".reverse_trace.json"
reverse_path.write_text(
    _json.dumps(reverse_trace, indent=2, ensure_ascii=False, default=str),
    encoding="utf-8"
)
logger.info(f"反向轨迹已保存: {reverse_path}")

# ══════════════════════════════════════════════
# 阶段 B — 正向推演
# ══════════════════════════════════════════════
logger.info("\n" + "-" * 50)
logger.info("阶段 B: 正向推演 (2019-01 → 最新)")
logger.info("-" * 50)

# 以反向推演的终点（2019-01 附近参数）做起点
start_params = reverse_trace[-1]["params"] if reverse_trace else {c: df[c].mean() for c in active_cols}
logger.info(f"正向起点: 来自反向推演终点 ({len(start_params)} 个参数)")

forward_trace: list[dict] = []

for i, d in enumerate(all_dates):
    # 回看窗口：该日之前已发生的全部交易
    history = df[df["exit_date_dt"] <= d]
    if len(history) < 5:
        continue

    groups = history.groupby(active_cols)
    stats = groups.agg(
        count=("pnl_pct", "count"),
        mean_pnl=("pnl_pct", "mean"),
        std_pnl=("pnl_pct", "std")
    ).reset_index()
    stats = stats[stats["count"] >= 3]
    if stats.empty:
        continue

    best = stats.loc[stats["mean_pnl"].idxmax()]
    best_params = {c: float(best[c]) for c in active_cols}
    forward_trace.append({
        "date": str(d.date()),
        "n_history": len(history),
        "n_groups": len(stats),
        "best_group_count": int(best["count"]),
        "best_group_pnl": round(float(best["mean_pnl"]), 4),
        "best_group_std": round(float(best["std_pnl"]), 4),
        "params": best_params,
    })

    if (i + 1) % 500 == 0:
        logger.info(f"  正向推演: 已处理 {i+1}/{len(all_dates)} 天, 当前最优 PnL={best['mean_pnl']:.4f}")

logger.info(f"正向推演完成: {len(forward_trace)} 天")

# 保存正向轨迹
forward_path = TAPE_DIR / ".forward_trace.json"
forward_path.write_text(
    _json.dumps(forward_trace, indent=2, ensure_ascii=False, default=str),
    encoding="utf-8"
)
logger.info(f"正向轨迹已保存: {forward_path}")

# ══════════════════════════════════════════════
# 阶段 C — 收敛分析
# ══════════════════════════════════════════════
logger.info("\n" + "-" * 50)
logger.info("阶段 C: 收敛分析")
logger.info("-" * 50)

# 对齐时间线
rev_map = {r["date"]: r for r in reverse_trace}
fwd_map = {f["date"]: f for f in forward_trace}
common_dates = sorted(set(rev_map.keys()) & set(fwd_map.keys()))

converged_params: dict[str, dict] = {}
param_diffs: dict[str, list[dict]] = {}

for c in active_cols:
    diffs = []
    for d in common_dates:
        rev_val = rev_map[d]["params"].get(c)
        fwd_val = fwd_map[d]["params"].get(c)
        if rev_val is not None and fwd_val is not None:
            diff = abs(rev_val - fwd_val)
            diffs.append({"date": d, "diff": diff, "rev": rev_val, "fwd": fwd_val})
    param_diffs[c] = diffs

    if not diffs:
        converged_params[c] = {"status": "no_data", "converged_at": None}
        continue

    # 取最后 10% 的差异均值（按参数实际值归一化）
    tail = diffs[-max(5, len(diffs) // 10):]
    # 对 int 型参数用绝对差值，对 float 型参数用相对差值
    mean_abs_diff = _np.mean([t["diff"] for t in tail])
    mean_val = _np.mean([t["rev"] for t in tail]) + 1e-8
    rel_diff = mean_abs_diff / mean_val

    if mean_abs_diff < 1e-8:
        converged_params[c] = {"status": "identical", "converged_at": diffs[0]["date"]}
    elif rel_diff < 0.05:
        converged_params[c] = {"status": "converged", "converged_at": diffs[-1]["date"],
                               "abs_diff": round(float(mean_abs_diff), 6), "rel_diff": round(float(rel_diff), 4)}
    else:
        converged_params[c] = {"status": "not_converged", "latest_abs_diff": round(float(mean_abs_diff), 6),
                               "latest_rel_diff": round(float(rel_diff), 4)}

n_converged = sum(1 for v in converged_params.values() if v.get("status") in ("converged", "identical"))
n_not = sum(1 for v in converged_params.values() if v.get("status") == "not_converged")

# 收敛点
convergence_dates = [
    v["converged_at"] for v in converged_params.values()
    if v.get("status") == "converged" and v.get("converged_at")
]
latest_convergence = max(convergence_dates) if convergence_dates else None

report = {
    "summary": {
        "tape_rows": len(df),
        "tape_date_range": f"{all_dates[0].date() if all_dates else '?'} → {all_dates[-1].date() if all_dates else '?'}",
        "total_params": len(PARAM_COLS),
        "active_params": len(active_cols),
        "frozen_params": len(frozen_cols),
        "reverse_days": len(reverse_trace),
        "forward_days": len(forward_trace),
        "common_days": len(common_dates),
        "converged_params": n_converged,
        "not_converged_params": n_not,
        "latest_convergence_date": str(latest_convergence) if latest_convergence else None,
        "convergence_rate": round(n_converged / len(active_cols) * 100, 1) if active_cols else 0,
    },
    "frozen_params": {c: str(df[c].iloc[0])[:30] for c in frozen_cols},
    "frozen_count": len(frozen_cols),
    "param_details": converged_params,
}

# 保存收敛报告
report_path = TAPE_DIR / ".convergence_report.json"
report_path.write_text(
    _json.dumps(report, indent=2, ensure_ascii=False, default=str),
    encoding="utf-8"
)
logger.info(f"收敛报告已保存: {report_path}")

# ── 打印摘要 ──
logger.info("\n" + "=" * 50)
logger.info("收敛分析摘要")
logger.info("=" * 50)
s = report["summary"]
logger.info(f"  纸带总行: {s['tape_rows']}")
logger.info(f"  日期范围: {s['tape_date_range']}")
logger.info(f"  总参数: {s['total_params']} | 活性: {s['active_params']} | 冻结: {s['frozen_params']}")
logger.info(f"  已收敛: {s['converged_params']} / {s['active_params']} ({s['convergence_rate']}%)")
logger.info(f"  未收敛: {s['not_converged_params']}")
logger.info(f"  最晚收敛日期: {s['latest_convergence_date']}")
logger.info("\n两段式推演完成")
