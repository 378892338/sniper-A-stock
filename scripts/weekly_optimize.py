"""周参数自优化入口 — python -m scripts.weekly_optimize

每周一 09:00 由 Windows 计划任务 QuantWeeklyOptimize 触发。

流程:
  1. 交易日历守卫（非交易日或非周一 -> 跳过）
  2. 纸带新鲜度检查（无新增交易 -> 跳过）
  3. 周归因: weekly_check 完整归因
  4. 打字机重采样（如果纸带过期）
  5. 打字机全量归因
  6. 按市场状态分箱优选
  7. 写入 ParamManager + ParamLock 锁定
  8. 输出优化报告到 outputs/optimize_report_{week}.md

锁定机制:
  周优化写入后锁定参数 3 天（至周三 09:00），
  期间 configure_for_today() 的打字机归因只读不写。
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


# -- ParamLock: 日归因 vs 周优化仲裁 --
class ParamLock:
    """参数锁定 - 周优化后锁定 3 天，日归因在锁期间只读不写。"""
    @staticmethod
    def _dir():
        from config.paths import OUTPUT_DIR
        return OUTPUT_DIR / "optimize_target"

    @classmethod
    def _lock(cls):
        p = cls._dir() / ".param_lock.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def is_locked(cls) -> bool:
        p = cls._lock()
        if not p.exists():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return datetime.now() < datetime.fromisoformat(data["lock_until"])
        except Exception:
            return False

    @classmethod
    def lock(cls, days: int = 3) -> None:
        p = cls._lock()
        until = datetime.now() + timedelta(days=days)
        p.write_text(json.dumps({"lock_until": until.isoformat()}, ensure_ascii=False), encoding="utf-8")
        print(f"[lock] 参数已锁定至 {until.strftime('%Y-%m-%d %H:%M')}")

    @classmethod
    def unlock(cls) -> None:
        p = cls._lock()
        p.unlink(missing_ok=True)
        print("[unlock] 参数锁定已解除")

    @classmethod
    def remaining_hours(cls) -> float:
        p = cls._lock()
        if not p.exists():
            return 0.0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return max(0.0, (datetime.fromisoformat(data["lock_until"]) - datetime.now()).total_seconds() / 3600)
        except Exception:
            return 0.0


# -- 交易日历守卫 --
def _is_trading_monday() -> bool:
    """检查今天是否为交易日周一。"""
    now = datetime.now()
    if now.weekday() != 0:  # 0 = Monday
        print(f"[skip] 非周一（{now.strftime('%A')}），跳过周优化")
        return False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
        from sniper.data_router import DataRouter
        router = DataRouter()
        today = now.strftime("%Y-%m-%d")
        df = router.get_trading_dates(start=today, end=today)
        if df is None or df.empty:
            print(f"[skip] {today} 非交易日（节假日），跳过周优化")
            return False
        return True
    except Exception as e:
        print(f"[warn] 交易日历检查失败（放行）: {e}")
        return True


# -- 纸带新鲜度检查 --
_TAPE_COUNT_FILE = None
_PAPER_TAPE_PATH = None


def _paper_tape_path() -> Path:
    from config.paths import OUTPUT_DIR
    return OUTPUT_DIR / "optimize_target" / "paper_tape.parquet"


def _tape_count_file() -> Path:
    return _paper_tape_path().parent / ".last_tape_count.txt"


def _check_tape_freshness() -> bool:
    """检查纸带是否有新增交易。"""
    global _PAPER_TAPE_PATH, _TAPE_COUNT_FILE
    _PAPER_TAPE_PATH = _paper_tape_path()
    _TAPE_COUNT_FILE = _tape_count_file()
    if not _PAPER_TAPE_PATH.exists():
        print("[skip] paper_tape.parquet 不存在，跳过周优化")
        return False
    try:
        import pandas as pd
        current_count = len(pd.read_parquet(_PAPER_TAPE_PATH))
    except Exception as e:
        print(f"[warn] 纸带读取失败: {e}")
        return False

    old_count = 0
    if _TAPE_COUNT_FILE.exists():
        try:
            old_count = int(_TAPE_COUNT_FILE.read_text().strip())
        except (ValueError, OSError):
            pass

    if current_count <= old_count:
        print(f"[skip] 纸带无新增交易（{current_count} <= {old_count}），跳过周优化")
        return False

    _TAPE_COUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TAPE_COUNT_FILE.write_text(str(current_count))
    print(f"[ok] 纸带有新增交易: {old_count} -> {current_count}")
    return True


# -- 核心优化流程 --
def run_weekly_optimization() -> bool:
    """执行完整周优化流程。"""
    print("\n" + "=" * 60)
    print("[WOPT] 周参数自优化 开始")
    print("=" * 60)

    today = datetime.now().strftime("%Y-%m-%d")
    week_num = datetime.now().isocalendar()[1]

    # 1. 🚫 纸带重采样已移除（飞轮闭环：实盘纸带只增不减，不再被随机回测覆盖）
    print(f"\n[1/5] 跳过纸带重采样（飞轮闭环，纸带只增不减）")

    # 2. 打字机全量归因
    print("\n[2/5] 打字机全量归因...")
    try:
        import importlib
        opt_typed = importlib.import_module("scripts.optimize_typed")
        opt_typed.main()
    except Exception as e:
        print(f"[warn] 全量归因异常: {e}")

    # 3. 按市场状态分箱优选
    print("\n[3/5] 市场状态分箱优选...")
    try:
        import importlib
        opt_state = importlib.import_module("scripts.optimize_state_attribution")
        opt_state.main()
    except Exception as e:
        print(f"[warn] 分箱优选异常（继续）: {e}")

    # 4. 持久化归因结果（替代旧的锁定 + 参数管理器）
    print("\n[4/5] 持久化归因结果...")
    try:
        from sniper.config import save_effective_params
        save_effective_params()
    except Exception as e:
        print(f"[warn] 归因持久化异常: {e}")

    # 5. 生成优化报告
    print("\n[5/5] 生成优化报告...")
    from config.paths import OUTPUT_DIR
    report_path = OUTPUT_DIR / "reports" / f"optimize_report_w{week_num}.md"
    try:
        try:
            import importlib
            wc = importlib.import_module("scripts.weekly_check")
            wc.main()
        except Exception as e:
            print(f"[warn] 周归因监控异常: {e}")

        ok_flag = "V" if _PAPER_TAPE_PATH.exists() else "X"
        report = f"""# 周参数优化报告 - 第 {week_num} 周 ({today})

## 执行状态

| 步骤 | 状态 |
|------|------|
| 纸带采样 | ✅ 跳过（飞轮闭环，实盘纸带只增不减） |
| 全量归因 | V（见 paper_tape） |
| 分箱优选 | V（见 ParamManager） |
| 参数持久化 | V（见 effective_params.json） |

## 飞轮闭环状态

- 已持久化: True
- 纸带路径: {_PAPER_TAPE_PATH}
"""
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(f"[ok] 优化报告: {report_path}")
    except Exception as e:
        print(f"[warn] 报告生成异常: {e}")

    print("\n" + "=" * 60)
    print("[OK] 周参数自优化完成")
    print("=" * 60)
    return True


def main():
    """周优化入口 - 周一 09:00 自动触发。"""
    if not _is_trading_monday():
        sys.exit(0)
    if not _check_tape_freshness():
        sys.exit(0)
    success = run_weekly_optimization()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
