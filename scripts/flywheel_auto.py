"""管道完成后自动飞轮注入 — 提取错误特征 → 去重 → 瞬态过滤 → 注入

在 run_pipeline.py 的 _run_single_day / _run_intraday / _run_intraday_light
的 finally 块中调用，自动捕获运行异常并注入飞轮经验库。

CLI 用法:
  python -m scripts.flywheel_auto --inject "[BUG-XXX] 描述"
  python -m scripts.flywheel_auto --from-git
"""

import hashlib
import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path

from core.logger import get_logger

logger = get_logger("scripts.flywheel_auto")

# ── 路径 ──
PROJECT = Path(__file__).resolve().parent.parent
LOG_FILE = str(PROJECT / "outputs/reports/pipeline.log")
INJECTOR = PROJECT.parent / ".ai/flywheel/inject_rule.js"
COMMITTER = PROJECT.parent / ".ai/flywheel/cli.js"
LESSONS = PROJECT / ".ai/memory/LESSONS.md"
CACHE_FILE = PROJECT / ".ai/memory/.transient_cache.json"

# ── 错误分类 ──
PATTERNS = [
    (r"所有源(均)?失败|所有数据源不可用", "[BUG-DATA-SOURCE]"),
    (r"白名单", "[BUG-WHITELIST]"),
    (r"覆盖率.*0\.0%", "[BUG-COVERAGE]"),
    (r"超时|Timeout|timed out", "[BUG-TIMEOUT]"),
    (r"泄漏|leak|泄露", "[BUG-LEAK]"),
    (r"Expecting value|HTTP.*fail|Connection.*refused|No objects to concatenate",
     "[BUG-API-FAIL]"),
]

WARN_KW = [
    "所有源", "不可用", "覆盖率.*0", "白名单",
    "超时", "失败", "异常", "跳过", "泄漏", "leak",
    "Expecting value", "HTTP.*fail",
]


def _classify(line: str) -> str:
    for pat, tag in PATTERNS:
        if re.search(pat, line, re.IGNORECASE):
            return tag
    return "[BUG-OTHER]"


def _read_existing_rules() -> set[str]:
    if not LESSONS.exists():
        return set()
    text = LESSONS.read_text("utf-8", errors="replace")
    sigs = set()
    for m in re.finditer(
        r"--- \[(ANCHOR_[^\]]+)\] ---\n(.*?)--- \[END_\1\] ---", text, re.DOTALL
    ):
        for line in m.group(2).split("\n"):
            line = line.strip()
            if line.startswith("[BUG-"):
                sigs.add(line)
    return sigs


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def _extract_errors(marker: str) -> list[str]:
    lp = Path(LOG_FILE)
    if not lp.exists():
        return []
    text = lp.read_text("utf-8", errors="replace")
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if marker in l), None)
    if start is None:
        return []
    results = []
    for line in lines[start:]:
        if "[ERROR]" in line:
            results.append(line.strip())
        elif "[WARNING]" in line:
            if any(re.search(k, line, re.IGNORECASE) for k in WARN_KW):
                results.append(line.strip())
    seen = set()
    unique = []
    for r in results:
        sig = hashlib.sha256(r[:80].encode()).hexdigest()[:16]
        if sig not in seen:
            seen.add(sig)
            unique.append(r)
    return unique


def _do_inject(rule_text: str) -> bool:
    """通过临时文件传递规则（避免 argv 截断/转义问题），返回是否成功"""
    try:
        if len(rule_text) < 10 or "[" not in rule_text or "]" not in rule_text:
            logger.warning(f"flywheel 规则太短或格式错误: {rule_text[:40]}")
            return False

        r1 = subprocess.run(
            ["node", str(INJECTOR), rule_text],
            cwd=str(PROJECT), timeout=15,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r1.returncode != 0:
            logger.warning(f"flywheel inject 失败 (exit={r1.returncode}): {r1.stderr.strip()[:200]}")
            return False

        r2 = subprocess.run(
            ["node", str(COMMITTER), "commit"],
            cwd=str(PROJECT), timeout=15,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r2.returncode != 0:
            logger.warning(f"flywheel commit 失败 (exit={r2.returncode}): {r2.stderr.strip()[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("flywheel inject/commit 超时")
        return False
    except Exception as e:
        logger.warning(f"flywheel 注入异常: {e}")
        return False


def run(log_path: str = LOG_FILE, marker: str = ""):
    """供 run_pipeline.py finally 调用。提取→去重→瞬态过滤→注入。"""
    global LOG_FILE
    if log_path:
        LOG_FILE = str(log_path) if not isinstance(log_path, str) else log_path
    try:
        errors = _extract_errors(marker)
        if not errors:
            return

        existing = _read_existing_rules()
        cache = _load_cache()
        now = int(time.time())
        to_inject = []

        for err in errors:
            tag = _classify(err)
            sig = hashlib.sha256(err[:80].encode()).hexdigest()[:16]
            key = f"{tag}:{sig}"
            rule_text = f"{key} {err[:200]}"

            if any(key in e for e in existing):
                continue

            entry = cache.get(key)

            if entry and entry.get("injected"):
                continue

            if entry is None:
                cache[key] = {"t": now, "text": rule_text}
            elif now - entry["t"] > 300:
                to_inject.append(rule_text)
                cache[key] = {"t": now, "text": rule_text, "injected": True}
            else:
                pass

        _save_cache(cache)

        for rule in to_inject:
            ok = _do_inject(rule)
            if ok:
                logger.info(f"flywheel 注入: {rule[:40]}")

    except Exception:
        pass


def inject_direct(rule_text: str) -> bool:
    """直接注入一条规则（供 CLI/Git 钩子/Claude 调用）"""
    return _do_inject(rule_text)


if __name__ == "__main__":
    import subprocess as _sp
    import argparse

    p = argparse.ArgumentParser(prog="python -m scripts.flywheel_auto")
    p.add_argument("--inject", help="直接注入: --inject \"[BUG-XXX] desc\"")
    p.add_argument("--from-git", action="store_true", help="从最近git提交提取规则")
    args = p.parse_args()

    if args.inject and args.from_git:
        p.error("--inject 和 --from-git 不能同时使用")

    if args.inject:
        ok = inject_direct(args.inject)
        print(f"[{'OK' if ok else 'FAIL'}] 注入: {args.inject[:60]}")

    elif args.from_git:
        try:
            msg = _sp.run(
                ["git", "log", "-1", "--pretty=%B"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
            if msg.returncode != 0:
                print("[FAIL] 非 git 仓库或 git 不可用")
                raise SystemExit(1)
            injected = 0
            for line in msg.stdout.split("\n"):
                line = line.strip()
                if "[BUG-" in line and "]" in line:
                    start = line.index("[")
                    end = line.index("]") + 1
                    tag = line[start:end]
                    if tag.startswith("[BUG-"):
                        ok = inject_direct(line)
                        if ok:
                            injected += 1
                            print(f"[OK] Git 注入: {line[:60]}")
            if injected == 0:
                print("[SKIP] 提交信息中无 [BUG-xxx] 标签")
        except FileNotFoundError:
            print("[FAIL] git 命令未找到")
