"""纸带归因验证脚本

模拟每天开盘前 configure_for_today() 的执行链路：
  load_paper_tape() → _find_neighbors() → _attribution() → 全局参数更新

用法:
  python scripts/verify_paper_tape.py
"""
import sys, json, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sniper.config as cfg
import pandas as pd
import numpy as np
from core.logger import get_logger

logger = get_logger("scripts.verify_paper_tape")
OUTPUT_DIR = Path("outputs/optimize_target")


def load_paper_tape() -> int:
    """加载纸带，返回交易笔数。"""
    cfg.load_paper_tape()
    n = len(cfg._TRADE_PAPER) if cfg._TRADE_PAPER else 0
    logger.info(f"纸带已加载: {n} 笔交易")
    return n


def verify_neighbors() -> dict:
    """验证最近邻查找。"""
    # 构造几种有代表性的市场指纹测距离和数量级
    test_cases = [
        ("均势市",  [58, 82, 35, 50]),    # 接近纸带均值
        ("强趋势",  [75, 95, 60, 80]),     # 高 L0 + 高趋势 + 高量能 + 高宽度
        ("弱趋势",  [35, 45, 20, 15]),     # 低 L0 + 低趋势 + 低量能 + 低宽度
        ("震荡市",  [50, 60, 30, 50]),     # 中间值
        ("极端缩量", [40, 50, 15, 10]),    # 极低量能
    ]
    results = {}
    for label, fp in test_cases:
        neighbors = cfg._find_neighbors(fp)
        n_neighbors = len(neighbors)
        avg_pnl = np.mean([t.get("pnl_pct", 0) for t in neighbors]) if neighbors else 0
        results[label] = {
            "fingerprint": fp,
            "neighbors": n_neighbors,
            "avg_pnl_pct": round(float(avg_pnl), 4),
            "ok": n_neighbors >= 10,
        }
        logger.info(f"  {label}: fp={fp} → 近邻={n_neighbors}笔, 平均pnl={avg_pnl:.4f}")
    return results


def verify_attribution(fp: list[float], label: str = "") -> dict:
    """验证归因链路：找近邻 → 归因 → 输出参数。"""
    neighbors = cfg._find_neighbors(fp)
    if len(neighbors) < 10:
        logger.warning(f"  {label}: 近邻不足（{len(neighbors)}笔），跳过归因")
        return {"ok": False, "reason": f"近邻不足（{len(neighbors)}笔）"}

    params = cfg._attribution(neighbors)
    logger.info(f"  {label}: 归因参数 = {params}")

    # B方案：无信号参数不输出是预期行为，不做完整性检查
    # 只对输出了的参数做合法性验证（范围检查）
    violations = []
    param_meta = {
        "stop_loss":        {"lo": -0.20, "hi": -0.02},
        "trailing_stop":    {"lo": -0.10, "hi": -0.02},
        "max_hold_days":    {"lo": 5,     "hi": 60},
        "position_size":    {"lo": 0.05,  "hi": 0.25},
        "soft_min_score":   {"lo": 48,    "hi": 80},
        "bullish_threshold":{"lo": 45,    "hi": 70},
    }
    for k, meta in param_meta.items():
        v = params.get(k)
        if v is None:
            continue  # B方案：无信号参数不输出，不报缺
        if not (meta["lo"] <= v <= meta["hi"]):
            violations.append(f"{k}={v} 超出 [{meta['lo']},{meta['hi']}]")

    ok = not violations
    status = "✅" if ok else "❌"
    issues = violations[:]

    result = {
        "ok": ok,
        "params": params,
        "n_neighbors": len(neighbors),
        "issues": issues,
        "status": status,
    }
    if issues:
        logger.warning(f"  {label}: {status} 问题: {issues}")
    else:
        logger.info(f"  {label}: {status} 所有输出参数合法")
    return result


def verify_configure_for_today(fp: list[float], label: str = "") -> dict:
    """验证完整链路：configure_for_today() → 全局参数被更新。"""
    # 记录更新前的参数
    before = {
        "stop_loss": cfg.EXIT.stop_loss,
        "trailing_stop": cfg.EXIT.trailing_stop,
        "max_hold_days": cfg.EXIT.max_hold_days,
        "position_size": cfg.RISK.position_size,
        "soft_min_score": cfg.ENTRY.soft_min_score,
        "bullish_threshold": cfg.MARKET.bullish_threshold,
    }

    cfg.configure_for_today(*fp)

    after = {
        "stop_loss": cfg.EXIT.stop_loss,
        "trailing_stop": cfg.EXIT.trailing_stop,
        "max_hold_days": cfg.EXIT.max_hold_days,
        "position_size": cfg.RISK.position_size,
        "soft_min_score": cfg.ENTRY.soft_min_score,
        "bullish_threshold": cfg.MARKET.bullish_threshold,
    }

    changed = {k: {"before": before[k], "after": after[k]}
               for k in before if before[k] != after[k]}
    ok = len(changed) > 0  # 至少改了点啥
    logger.info(f"  {label}: {len(changed)} 个参数变更: {changed if changed else '无变更'}")
    return {"ok": ok, "changed": changed, "n_changed": len(changed)}


def verify_re_extract():
    """验证 --re-extract 模式能否正常工作。"""
    import subprocess
    logger.info("验证 --re-extract 模式...")
    result = subprocess.run(
        [sys.executable, "scripts/optimize_target.py", "--start", "2026-05-01",
         "--end", "2026-06-04", "--n-samples", "2", "--re-extract"],
        capture_output=True, text=True, timeout=300,
    )
    ok = "纸带已写入" in result.stdout
    if ok:
        logger.info("  ✅ --re-extract 模式正常")
    else:
        logger.warning(f"  ❌ --re-extract 异常: {result.stderr[:500]}")
    return {"ok": ok, "stdout": result.stdout[-300:], "stderr": result.stderr[-300:]}


if __name__ == "__main__":
    logger.info(f"\n{'='*60}")
    logger.info(f"打字机归因验证")
    logger.info(f"{'='*60}")

    # 1. 加载纸带
    n = load_paper_tape()
    assert n > 0, "纸带为空!"

    # 2. 验证最近邻查找
    logger.info(f"\n--- 1/4: 最近邻查找 ---")
    neighbor_results = verify_neighbors()

    # 3. 验证归因
    logger.info(f"\n--- 2/4: 归因参数 ---")
    attribution_results = {}
    for label, fp in [
        ("均势市", [58, 82, 35, 50]),
        ("强趋势", [75, 95, 60, 80]),
        ("弱趋势", [35, 45, 20, 15]),
    ]:
        attribution_results[label] = verify_attribution(fp, label)

    # 4. 验证全局参数更新
    logger.info(f"\n--- 3/4: 全局参数传播 ---")
    update_results = {}
    for label, fp in [
        ("均势市-更新", [58, 82, 35, 50]),
        ("强趋势-更新", [75, 95, 60, 80]),
    ]:
        update_results[label] = verify_configure_for_today(fp, label)

    # 5. 验证 --re-extract
    logger.info(f"\n--- 4/4: --re-extract 模式 ---")
    re_extract_result = verify_re_extract()

    # ── 汇总 ──
    logger.info(f"\n{'='*60}")
    logger.info(f"验证汇总")
    logger.info(f"{'='*60}")

    all_ok = True
    checks = [("纸带加载", n > 0, f"{n} 笔交易")]
    checks += [(f"  最近邻-{k}", v["ok"], f"{v['neighbors']}笔") for k, v in neighbor_results.items()]
    checks += [(f"  归因-{k}", v["ok"], f"{len(v.get('issues',[]))} 个问题" if not v["ok"] else "通过")
               for k, v in attribution_results.items()]
    checks += [(f"  更新-{k}", v["ok"], f"{v['n_changed']} 个变更")
               for k, v in update_results.items()]
    checks += [("  --re-extract", re_extract_result["ok"], "通过" if re_extract_result["ok"] else "失败")]

    for label, ok, detail in checks:
        icon = "✅" if ok else "❌"
        logger.info(f"  {icon} {label}: {detail}")
        if not ok:
            all_ok = False

    logger.info(f"\n{'='*60}")
    if all_ok:
        logger.info(f"✅ 所有验证通过 — 打字机归因链路工作正常")
    else:
        logger.warning(f"❌ 存在未通过项，请检查上述日志")
    logger.info(f"{'='*60}")
