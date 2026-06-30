"""管线排水脚本 — 健康检查 + 深度清理 + 诊断报告

用法:
  python -m scripts.pipeline_drain --check    # 健康检查
  python -m scripts.pipeline_drain --flush    # 深度清理
  python -m scripts.pipeline_drain --status   # 诊断报告

可作为 Windows 计划任务在 15:30 定时调用（管线运行前清理）。
"""

import sys
import json
import time
from pathlib import Path

# 确保项目根目录在路径中
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.logger import get_logger

logger = get_logger("scripts.pipeline_drain")

# ── 路径常量 ──
PIPELINE_LOCK = ROOT / "outputs/.pipeline.lock"
INTRADAY_LOCK = ROOT / "outputs/.intraday.lock"
LAST_RUN_FILE = ROOT / "outputs/reports/last_run.json"
SKIP_LIST_FILE = ROOT / "outputs/reports/skip_list.json"
PIPELINE_LOG = ROOT / "outputs/reports/pipeline.log"


def _pid_alive(pid: int) -> bool:
    """检查 PID 是否在运行（Windows tasklist）。"""
    import subprocess as _sp
    try:
        result = _sp.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=5,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _check_lock(lock_path: Path, name: str) -> dict:
    """检查单个锁文件的状态。"""
    result = {"path": str(lock_path), "exists": lock_path.exists(), "stale": False, "pid": None}
    if not result["exists"]:
        return result
    try:
        parts = lock_path.read_text(encoding="utf-8").strip().split()
        pid = int(parts[0])
        result["pid"] = pid
        if not _pid_alive(pid):
            result["stale"] = True
    except (ValueError, IndexError, OSError):
        result["stale"] = True
    return result


def _check():
    """健康检查：锁悬挂、日志大小、进程残留。"""
    issues = []

    # 锁检查
    for lock_path, name in [(PIPELINE_LOCK, "pipeline"), (INTRADAY_LOCK, "intraday")]:
        state = _check_lock(lock_path, name)
        if state["exists"]:
            if state["stale"]:
                issues.append(f"⚠️  {name} 锁僵死 (PID={state['pid']})")
                print(f"[{name}] 锁文件存在但 PID 不在运行 → 僵死")
            else:
                print(f"[{name}] 锁文件存在，PID={state['pid']} 正常运行")
        else:
            print(f"[{name}] 无锁文件")

    # 日志大小
    if PIPELINE_LOG.exists():
        size_mb = PIPELINE_LOG.stat().st_size / 1024 / 1024
        print(f"[日志] pipeline.log: {size_mb:.1f}MB")
        if size_mb > 20:
            issues.append(f"⚠️  pipeline.log 过大 ({size_mb:.1f}MB)")
    else:
        print(f"[日志] pipeline.log 不存在")

    # last_run 状态
    if LAST_RUN_FILE.exists():
        try:
            data = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
            print(f"[状态] full={data.get('full', 'N/A')}, intraday={data.get('intraday', 'N/A')}")
        except Exception:
            issues.append("⚠️  last_run.json 损坏")
    else:
        issues.append("⚠️  last_run.json 不存在")

    # skip_list
    if SKIP_LIST_FILE.exists():
        try:
            data = json.loads(SKIP_LIST_FILE.read_text(encoding="utf-8"))
            skip_count = len(data.get("skip_set", {}))
            fail_count = len(data.get("fail_counts", {}))
            print(f"[跳过] {skip_count} 只跳过, {fail_count} 只有失败记录")
        except Exception:
            issues.append("⚠️  skip_list.json 损坏")

    print()
    if issues:
        print("发现的问题:")
        for i in issues:
            print(f"  {i}")
        return False
    print("✅ 一切正常")
    return True


def _flush():
    """深度清理：杀僵尸、清锁、复位 skip_list。"""
    cleaned = 0

    # 1. 清理僵死锁
    for lock_path, name in [(PIPELINE_LOCK, "pipeline"), (INTRADAY_LOCK, "intraday")]:
        state = _check_lock(lock_path, name)
        if state["exists"] and state["stale"]:
            try:
                lock_path.unlink()
                print(f"  ✅ 已清理 {name} 僵死锁 (PID={state['pid']})")
                cleaned += 1
            except OSError as e:
                print(f"  ❌ 清理 {name} 锁失败: {e}")

    # 2. 清理游离 .tmp 文件
    try:
        tmp_count = 0
        for f in Path("outputs").rglob("*.tmp"):
            try:
                f.unlink()
                tmp_count += 1
            except OSError:
                pass
        if tmp_count:
            print(f"  ✅ 已清理 {tmp_count} 个临时文件")
            cleaned += 1
    except Exception as e:
        print(f"  ❌ 临时文件清理异常: {e}")

    # 3. 日志轮转（仅 pipeline.log > 20MB）
    if PIPELINE_LOG.exists():
        size_mb = PIPELINE_LOG.stat().st_size / 1024 / 1024
        if size_mb > 20:
            import shutil
            from datetime import datetime
            try:
                archive_name = f"pipeline.log.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                archive_path = PIPELINE_LOG.with_name(archive_name)
                shutil.copy2(PIPELINE_LOG, archive_path)
                # 清空原文件（FileHandler 仍持有 fd，清空后继续写入新内容）
                with open(PIPELINE_LOG, "w", encoding="utf-8") as f:
                    f.truncate(0)
                print(f"  ✅ 日志已轮转: {archive_name} ({size_mb:.1f}MB → 清空)")
                cleaned += 1
            except Exception as e:
                print(f"  ❌ 日志轮转失败: {e}")

    # 4. skip_list 过期清理
    try:
        from shared.source_reliability import StockSkipTracker
        skip = StockSkipTracker()
        n = skip.prune_expired()
        if n:
            print(f"  ✅ skip_list 已过期 {n} 只")
            cleaned += 1
    except Exception as e:
        print(f"  ❌ skip_list 清理异常: {e}")

    print()
    if cleaned:
        print(f"深度清理完成: {cleaned} 项已处理")
    else:
        print("深度清理完成: 无需处理")
    return True


def _status():
    """打印完整诊断报告。"""
    print("=" * 50)
    print("  量化管线诊断报告")
    print("=" * 50)
    print()

    # 进程检查
    print("── 进程状态 ──")
    import subprocess as _sp
    try:
        result = _sp.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l for l in result.stdout.split("\n") if "run_pipeline" in l.lower() or "serve_report" in l.lower() or "run_postclose" in l.lower()]
        for l in lines:
            print(f"  {l.strip()}")
        if not lines:
            print("  无活跃的管线 Python 进程")
    except Exception:
        print("  (无法查询进程)")

    print()
    print("── 锁状态 ──")
    for lock_path, name in [(PIPELINE_LOCK, "pipeline"), (INTRADAY_LOCK, "intraday")]:
        s = _check_lock(lock_path, name)
        if s["exists"]:
            tag = "【僵死】" if s["stale"] else ""
            print(f"  {name}: 存在 PID={s['pid']} {tag}")
        else:
            print(f"  {name}: 无")

    print()
    print("── 运行记录 ──")
    if LAST_RUN_FILE.exists():
        try:
            data = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
            print(f"  全量管道最后成功: {data.get('full', 'N/A')}")
            print(f"  盘中初稿最后成功: {data.get('intraday', 'N/A')}")
        except Exception:
            print("  last_run.json 损坏")
    else:
        print("  last_run.json 不存在")

    print()
    print("── 数据源跳过列表 ──")
    if SKIP_LIST_FILE.exists():
        try:
            data = json.loads(SKIP_LIST_FILE.read_text(encoding="utf-8"))
            skip_set = data.get("skip_set", {})
            skip_count = len(skip_set)
            if skip_count:
                top = sorted(skip_set.items(), key=lambda x: -x[1])[:5]
                print(f"  当前跳过 {skip_count} 只股票")
                for sym, ts in top:
                    age_h = (time.time() - ts) / 3600
                    print(f"    {sym}: {age_h:.0f}h 前加入跳过")
            else:
                print("  无跳过股票")
        except Exception:
            print("  损坏")
    else:
        print("  不存在")

    print()
    print("── 日志 ──")
    if PIPELINE_LOG.exists():
        size_mb = PIPELINE_LOG.stat().st_size / 1024 / 1024
        print(f"  pipeline.log: {size_mb:.1f}MB")
    else:
        print("  pipeline.log: 不存在")

    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="量化管线排水脚本")
    parser.add_argument("--check", action="store_true", help="健康检查")
    parser.add_argument("--flush", action="store_true", help="深度清理")
    parser.add_argument("--status", action="store_true", help="诊断报告")
    args = parser.parse_args()

    if args.check:
        ok = _check()
        sys.exit(0 if ok else 1)
    elif args.flush:
        _flush()
        sys.exit(0)
    elif args.status:
        _status()
        sys.exit(0)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
