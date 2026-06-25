"""循环归因：为每个市场状态找到最优参数，记录完整环境指纹

输出:
  - outputs/optimize_target/state_params.json — 各市场状态→最优参数 映射表
  - outputs/optimize_target/state_attribution_detail.parquet — 归因全量明细
"""

import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ['NUMEXPR_MAX_THREADS'] = '1'

import numpy as np
import pandas as pd

import sniper.config as cfg
from core.logger import get_logger
logger = get_logger("scripts.optimize_state_attribution")

OUTPUT_DIR = Path("outputs/optimize_target")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 加载纸带 ──
cfg.load_paper_tape()
tape = cfg._TRADE_PAPER
logger.info(f"纸带加载: {len(tape)} 笔交易")

# ── 按 L0 评分 + 子维度聚类 ──
# 把 L0 空间切 10 个箱（不是 3 个），每个箱内再按 trend/volume/breadth 细分
L0_BINS = [0, 20, 30, 40, 45, 50, 55, 60, 70, 80, 100]
L0_LABELS = ["极熊", "熊市", "偏熊", "偏熊中性", "中性", "偏牛中性", "中性偏强", "偏牛", "牛市", "极牛"]

def _fingerprint_label(fp):
    """给市场指纹打标签。"""
    l0 = fp[0] if isinstance(fp, (list, tuple)) else 50
    if l0 >= 80: return "极牛"
    if l0 >= 70: return "牛市"
    if l0 >= 60: return "偏牛"
    if l0 >= 55: return "中性偏强"
    if l0 >= 50: return "偏牛中性"
    if l0 >= 45: return "中性"
    if l0 >= 40: return "偏熊中性"
    if l0 >= 30: return "偏熊"
    if l0 >= 20: return "熊市"
    return "极熊"

# 为每笔交易打上市场状态标签
for t in tape:
    fp = t.get("market_state_vector", [50, 50, 50, 50])
    t["state_label"] = _fingerprint_label(fp)

# 按状态分组归因
results = []
state_params_map = {}

for label in L0_LABELS:
    group = [t for t in tape if t["state_label"] == label]
    if len(group) < 10:
        logger.info(f"[{label}] 样本不足 ({len(group)})，跳过")
        continue

    # 计算该组的平均市场指纹
    fpg = np.mean([t["market_state_vector"] for t in group], axis=0).tolist()

    # 运行完整减枝归因
    try:
        params = cfg._attribution(group)
    except Exception as e:
        logger.warning(f"[{label}] 归因失败: {e}")
        continue

    # 统计该组的交易表现
    pnls = [t["pnl_pct"] for t in group]
    win_rates = [t["pnl_pct"] for t in group if t["pnl_pct"] > 0]

    entry = {
        "state_label": label,
        "avg_fingerprint": fpg,
        "trades_count": len(group),
        "avg_pnl": float(np.mean(pnls)),
        "median_pnl": float(np.median(pnls)),
        "win_rate": len(win_rates) / max(len(group), 1),
        "std_pnl": float(np.std(pnls)),
        "optimal_params": params,
    }
    state_params_map[label] = entry
    results.append(entry)
    logger.info(f"[{label}] 交易={len(group)}笔, 平均PnL={np.mean(pnls):+.2%}, "
                f"参数: {params}")

# 保存状态→参数 映射表（给 configure_for_today 用）
mapping = {}
for r in results:
    mapping[r["state_label"]] = {
        "avg_fingerprint": r["avg_fingerprint"],
        "n_trades": r["trades_count"],
        "avg_pnl": round(r["avg_pnl"], 6),
        "win_rate": round(r["win_rate"], 4),
        "params": r["optimal_params"],
    }

# 保存详细归因记录
detail = []
for label, entry in state_params_map.items():
    detail.append(entry)

df_detail = pd.DataFrame(detail)
detail_path = OUTPUT_DIR / "state_attribution_detail.parquet"
if not df_detail.empty:
    df_detail.to_parquet(detail_path, index=False)
    logger.info(f"归因明细已保存: {detail_path}")

(OUTPUT_DIR / "state_params.json").write_text(
    json.dumps({
        "generated_at": pd.Timestamp.now().isoformat(),
        "paper_tape_trades": len(tape),
        "state_mapping": mapping,
    }, ensure_ascii=False, indent=2),
    encoding="utf-8")

# ── 辅助：缺参时显示"—"而非 0.00 ──
def _p(v):
    return f"{v:<+9.2f}" if v is not None else "     —"

# ── 打印完整报告 ──
print("\n" + "="*80)
print("  各市场状态最优参数映射表")
print("="*80)
print(f"{'状态':<10} {'交易数':<6} {'平均PnL':<10} {'胜率':<8} {'stop_loss':<10} {'trailing':<10} {'profit_':<10} {'max_hold':<10} {'pos_sz':<8} {'min_scr':<8} {'bull_th':<8}")
print("-"*80)
for r in sorted(results, key=lambda x: L0_LABELS.index(x['state_label'])):
    p = r['optimal_params']
    print(f"{r['state_label']:<10} {r['trades_count']:<6} {r['avg_pnl']:<+9.2%} {r['win_rate']:<7.1%} "
          f"{_p(p.get('stop_loss'))} {_p(p.get('trailing_stop'))} "
          f"{_p(p.get('max_hold_days'))} {_p(p.get('position_size'))} {_p(p.get('soft_min_score'))} {_p(p.get('bullish_threshold'))}")

# ── 参数变化趋势分析（参数随时间/市场状态的演变） ──
print("\n" + "="*80)
print("  参数随市场状态的演变趋势")
print("="*80)
for pk in ["stop_loss", "trailing_stop", "position_size", "soft_min_score"]:
    vals = [(r["state_label"], r["optimal_params"].get(pk)) for r in results]
    # 投影片
    trend = " → ".join([f"{l}={v}" for l, v in vals if v is not None])
    print(f"  {pk}: {trend}")

print(f"\n映射表已保存到: {OUTPUT_DIR / 'state_params.json'}")