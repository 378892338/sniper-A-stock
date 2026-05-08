"""每日自动运行入口 — 数据更新 + 日报生成"""
import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
LOG_DIR = ROOT / "outputs" / "reports"
LOG_DIR.mkdir(parents=True, exist_ok=True)
from datetime import datetime
LOG_FILE = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d')}.txt"

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except PermissionError:
        # 文件被锁时写入备用日志
        alt = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(alt, "a", encoding="utf-8") as f:
            f.write(line + "\n")

log("=== Daily run start ===")

# Step 1: Update data
log("[1/2] Updating data...")
r1 = subprocess.run([sys.executable, str(ROOT / "scripts" / "update_data.py")], cwd=str(ROOT))
if r1.returncode != 0:
    log("Data update FAILED")
else:
    log("[1/2] Data update OK")

# Step 2: Generate report
log("[2/2] Generating report...")
r2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_strategy.py")], cwd=str(ROOT))
if r2.returncode != 0:
    log("Report generation FAILED")
else:
    log("[2/2] Report generation OK")

status = "SUCCESS" if (r1.returncode == 0 and r2.returncode == 0) else "FAILED"
log(f"=== Daily run {status} ===")
