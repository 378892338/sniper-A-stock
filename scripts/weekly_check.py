"""周末归因监控 — python -m scripts.weekly_check

读取纸带 → 重新归因 → 对比上周参数 → 打印参数漂移报告。

用法:
  python -m scripts.weekly_check                 # 默认
  python -m scripts.weekly_check --tape path     # 指定纸带路径
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import json
import os

import sniper.config as CFG
from core.logger import get_logger

logger = get_logger("scripts.weekly_check")

# 上次检查结果缓存
_DRIFT_CACHE = {}  # param_name → list[prev_direction]
_DRIFT_FILE = Path("outputs/optimize_target/drift_cache.json")


def verify_attribution_direction(trades: list[dict],
                                 attribution_result: dict) -> dict:
    """纸带方向验证 — 独立于归因的方向一致性检查。

    对 attribution_result 中有信号的每个参数 k：
      1. 从 trades 提取 vals 和 pnls
      2. 按 vals 中位数分 A 组（>中位数）和 B 组（≤中位数）
      3. 对比 A/B 组平均 PnL 与 impact 方向的一致性

    验证方法（中位数分组 + 平均 PnL 比较）与归因方法（win_median - lose_median）
    不同，不是自证循环。

    Args:
        trades: 纸带交易列表
        attribution_result: _attribution() 的输出（有信号的参数）

    Returns:
        dict: {k: {"direction": str, "verified": bool, "note": str}, ...}
    """
    if not attribution_result:
        return {}

    import numpy as np
    results: dict = {}

    for k in attribution_result:
        vals = np.array([t["params"].get(k, 0) for t in trades], dtype=float)
        pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)

        # 过滤 NaN
        valid = ~np.isnan(pnls)
        vals = vals[valid]
        pnls = pnls[valid]

        if len(vals) < 10:
            results[k] = {
                "direction": "样本不足",
                "verified": False,
                "note": f"仅 {len(vals)} 笔有效交易",
            }
            continue

        # 重新算 impact（attribution_result 只存了最终取值）
        pi = CFG.profit_impact(trades, k)
        impact = pi["impact"]

        if abs(impact) < 1e-8:
            results[k] = {
                "direction": "无区分度",
                "verified": True,
                "note": "|impact|≈0，验证跳过（不开枪原则）",
            }
            continue

        direction = "偏大有利" if impact > 0 else "偏小有利"

        # 按 vals 中位数分组
        median_val = float(np.median(vals))
        group_a = pnls[vals > median_val]  # 大值侧
        group_b = pnls[vals <= median_val]  # 小值侧

        if len(group_a) < 3 or len(group_b) < 3:
            results[k] = {
                "direction": direction,
                "verified": False,
                "note": f"分组样本不足: A={len(group_a)} B={len(group_b)}",
            }
            continue

        mean_a = float(group_a.mean())
        mean_b = float(group_b.mean())

        # 验证方向
        if impact > 0:
            verified = mean_a > mean_b
        else:
            verified = mean_a < mean_b

        note = (
            f"A组均值={mean_a:.4f} B组均值={mean_b:.4f}"
            if verified
            else f"方向不一致: A组均值={mean_a:.4f} B组均值={mean_b:.4f}"
        )

        results[k] = {
            "direction": direction,
            "verified": verified,
            "note": note,
        }

    return results


def _load_drift_cache():
    global _DRIFT_CACHE
    if _DRIFT_FILE.exists():
        try:
            _DRIFT_CACHE = json.loads(_DRIFT_FILE.read_text(encoding="utf-8"))
        except Exception:
            _DRIFT_CACHE = {}
    else:
        _DRIFT_CACHE = {}


def _save_drift_cache():
    _DRIFT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DRIFT_FILE.write_text(json.dumps(_DRIFT_CACHE, ensure_ascii=False, indent=2), encoding="utf-8")


def weekend_check(tape_path: str = "") -> dict:
    """每周归因检查，打印参数漂移报告。

    Args:
        tape_path: 纸带路径，为空则使用默认路径。

    Returns:
        漂移报告 dict: {param_name: {"direction": str, "weeks": int, "summary": str}}
    """
    path = tape_path or os.path.join("outputs", "optimize_target", "paper_tape.parquet")
    if not os.path.exists(path):
        logger.warning(f"纸带不存在: {path}")
        return {}

    # 加载纸带
    CFG.load_paper_tape(path)
    if CFG._TRADE_PAPER is None or len(CFG._TRADE_PAPER) < 10:
        logger.warning(f"纸带不足 10 笔: {len(CFG._TRADE_PAPER or [])} 笔")
        return {}

    # 完整归因
    trades = CFG._TRADE_PAPER
    today_params = CFG._attribution(trades)

    # 方向验证（独立于归因的验证方法）
    direction_check = verify_attribution_direction(trades, today_params)
    if direction_check:
        logger.info(f"\n--- 方向验证 ---")
        for k, v in direction_check.items():
            icon = "✅" if v["verified"] else "❌"
            logger.info(f"  {icon} {k}: {v['direction']} — {v['note']}")
        if all(v["verified"] for v in direction_check.values()):
            logger.info("✅ 所有信号参数方向验证通过")
        else:
            logger.warning("❌ 存在方向不一致的参数")

    # 加载漂移历史
    _load_drift_cache()

    # 逐参数对比
    drift_report = {}
    for k, v in today_params.items():
        prev_directions = _DRIFT_CACHE.get(k, [])
        prev_val = prev_directions[-1] if prev_directions else ""

        # 与 config 默认值对比，判断方向
        default = getattr(getattr(CFG, {
            "stop_loss": "EXIT", "trailing_stop": "EXIT",
            "max_hold_days": "EXIT", "position_size": "RISK",
            "soft_min_score": "ENTRY", "bullish_threshold": "MARKET",
        }.get(k, "EXIT")), k, None)
        direction = ""
        if default is not None and isinstance(v, (int, float)) and isinstance(default, (int, float)):
            if v > default * 1.1:
                direction = "up"
            elif v < default * 0.9:
                direction = "down"
            else:
                direction = "stable"

        # 更新漂移历史
        if prev_val == direction:
            _DRIFT_CACHE.setdefault(k, []).append(direction)
        else:
            _DRIFT_CACHE[k] = [direction]

        weeks = len(_DRIFT_CACHE[k])
        drift_report[k] = {
            "direction": direction,
            "weeks": weeks,
            "current_value": v,
            "default_value": default,
            "note": f"连续 {weeks} 周 {'向' + direction if direction != 'stable' else '稳定'}",
        }

    _save_drift_cache()

    # 打印报告
    logger.info(f"{'='*60}")
    logger.info(f"纸带归因报告: 纸带共 {len(trades)} 笔交易")
    logger.info(f"{'='*60}")
    for k, info in sorted(drift_report.items()):
        direction_icon = {"up": "↑", "down": "↓", "stable": "→"}.get(info["direction"], "?")
        logger.info(f"  {direction_icon} {k}: {info['current_value']} "
                    f"(默认={info['default_value']}) {info['note']}")

    # 标记连续 2+ 周漂移
    for k, info in drift_report.items():
        if info["direction"] in ("up", "down") and info["weeks"] >= 2:
            logger.info(f"  ⚠  {k} 连续 {info['weeks']} 周{info['direction']}漂移 "
                        f"({info['current_value']} vs 默认={info['default_value']})")

    logger.info(f"{'='*60}")
    return drift_report


def main():
    import argparse

    a = argparse.ArgumentParser(description="周末归因监控")
    a.add_argument("--tape", type=str, default="", help="纸带路径")
    args = a.parse_args()
    weekend_check(args.tape or "")


if __name__ == "__main__":
    main()
