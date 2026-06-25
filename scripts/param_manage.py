#!/usr/bin/env python3
"""参数管理器 CLI — 查看 / 修改 / 回滚 / 导出 / 导入参数"""

import sys
import json
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from sniper.engine.param_manager import ParamManager, DriftDetector, DEFAULT_PARAMS
from sniper.engine.online_learner import OnlineParamLearner
from sniper.engine.trade_log import TradeLogStore
from sniper.engine.evaluator import PerformanceEvaluator


def cmd_show(pm: ParamManager, drift: DriftDetector):
    """显示当前参数。"""
    params = pm.get()
    print("\n" + "=" * 60)
    print("  当前参数")
    print("=" * 60)

    groups = [
        ("风控参数", ["stop_loss", "trailing_stop", "max_hold_days", "position_size"]),
        ("市场状态", ["bullish_threshold", "bearish_threshold"]),
        ("入场过滤", ["soft_min_score", "soft_sector_top"]),
    ]

    for group_name, keys in groups:
        print(f"\n  ── {group_name} ──")
        for k in keys:
            val = params.get(k, "?")
            default = DEFAULT_PARAMS.get(k, "?")
            mark = " ★" if val != default else ""
            print(f"  {k:25s}: {val!s:<10}{mark}")

    print("\n  ★ = 已偏离默认值")

    # 漂移检查
    print(f"\n  {drift.summary(params)}")

    # 最近变更
    history = pm.get_history(limit=5)
    if history:
        print(f"\n  ── 最近 {len(history)} 条变更 ──")
        for h in history:
            print(f"  {h['changed_at'][:16]} | {h['key']:20s}: {h['old_value']} → {h['new_value']} ({h['trigger']})")


def cmd_set(pm: ParamManager, key: str, value: str):
    """修改参数。"""
    try:
        v = float(value)
        pm.set(key, v, trigger="cli")
        print(f"  ✅ {key} = {v}")
    except ValueError:
        print(f"  ❌ 无效数值: {value}")


def cmd_rollback(pm: ParamManager, steps: int = 1):
    """回滚参数。"""
    n = pm.rollback(steps)
    print(f"  ✅ 回滚 {n} 项, {steps} 步")


def cmd_history(pm: ParamManager):
    """显示参数变更历史。"""
    history = pm.get_history(limit=50)
    if not history:
        print("  无变更历史")
        return
    print(f"\n{'日期':<20} {'参数':<20} {'旧值':<10} {'新值':<10} {'触发':<10}")
    print("-" * 70)
    for h in history:
        print(f"{h['changed_at'][:16]:<20} {h['key']:<20} {h['old_value']:<10} {h['new_value']:<10} {h['trigger']:<10}")


def cmd_export(pm: ParamManager, path: str = ""):
    """导出参数。"""
    fpath = pm.export(path or None)
    print(f"  ✅ 参数已导出: {fpath}")


def cmd_import(pm: ParamManager, path: str):
    """导入参数。"""
    try:
        n = pm.import_from(path)
        print(f"  ✅ 导入 {n} 项参数")
    except FileNotFoundError:
        print(f"  ❌ 文件不存在: {path}")
    except json.JSONDecodeError:
        print(f"  ❌ JSON 格式错误: {path}")


def cmd_learn(learner: OnlineParamLearner):
    """显示在线学习状态。"""
    s = learner.summary()
    print(f"\n  在线学习器")
    print(f"  观测数:    {s['n_observations']}")
    print(f"  参数数:    {s['n_params']}")
    print(f"  最敏感参数:")
    for k, v, d in s['top_sensitive']:
        print(f"    {k:20s}: {v:+.4f} ({d})")
    print(f"\n  系数:")
    for k, v in sorted(s['coefficients'].items(), key=lambda x: abs(x[1]), reverse=True):
        print(f"    {k:20s}: {v:+.4f}")


def cmd_suggest(learner: OnlineParamLearner):
    """显示参数调整建议。"""
    suggestions = learner.suggest()
    if "note" in suggestions:
        print(f"  ℹ️ {suggestions['note']}")
        return
    print(f"\n  参数调整建议:")
    print(f"  {'参数':<20} {'敏感度':<10} {'方向':<10} {'置信度':<10}")
    print("-" * 50)
    for k, s in suggestions.items():
        if isinstance(s, dict):
            dir_symbol = "↑" if s["direction"] == "increase" else "↓" if s["direction"] == "decrease" else "→"
            print(f"  {k:<20} {s['sensitivity']:<10.4f} {dir_symbol:<10} {s['confidence']:<10.2f}")


def cmd_trades(trade_log: TradeLogStore):
    """显示交易统计。"""
    stats = trade_log.stats()
    print(f"\n  交易日志")
    print(f"  总交易数: {stats['total_trades']}")
    print(f"  文件数:   {stats['total_files']}")
    print(f"  存储位置: {stats['db_path']}")


def cmd_eval(evaluator: PerformanceEvaluator):
    """显示评估报告。"""
    reports = evaluator.load_reports()
    if reports.empty:
        print("  无评估报告")
        return
    latest = reports.iloc[-1]
    print(f"\n  最新评估: {latest.get('date', '?')} ({latest.get('period', '?')})")
    metrics = latest.get("metrics", {})
    if metrics:
        print(f"  月收益:    {metrics.get('month_return', 0):+.2%}")
        print(f"  夏普:     {metrics.get('sharpe', 0):.3f}")
        print(f"  最大回撤:  {metrics.get('max_drawdown', 0):.2%}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="参数管理器 CLI")
    parser.add_argument("action", nargs="?", default="show",
                        choices=["show", "set", "rollback", "history",
                                 "export", "import", "learn", "suggest",
                                 "trades", "eval", "drift"])
    parser.add_argument("key", nargs="?", default="", help="参数名")
    parser.add_argument("value", nargs="?", default="", help="参数值")
    parser.add_argument("--steps", type=int, default=1, help="回滚步数")
    parser.add_argument("--path", default="", help="文件路径")
    args = parser.parse_args()

    pm = ParamManager()
    drift = DriftDetector()
    learner = OnlineParamLearner()
    trade_log = TradeLogStore()
    evaluator = PerformanceEvaluator()

    if args.action == "show":
        cmd_show(pm, drift)
    elif args.action == "set":
        cmd_set(pm, args.key, args.value)
    elif args.action == "rollback":
        cmd_rollback(pm, args.steps)
    elif args.action == "history":
        cmd_history(pm)
    elif args.action == "export":
        cmd_export(pm, args.path)
    elif args.action == "import":
        cmd_import(pm, args.path)
    elif args.action == "learn":
        cmd_learn(learner)
    elif args.action == "suggest":
        cmd_suggest(learner)
    elif args.action == "trades":
        cmd_trades(trade_log)
    elif args.action == "eval":
        cmd_eval(evaluator)
    elif args.action == "drift":
        params = pm.get()
        print(f"\n  {drift.summary(params)}")


if __name__ == "__main__":
    main()
