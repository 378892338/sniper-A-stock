"""轻量日报服务 — 本地 HTTP 服务，支持刷新日报

用法:
  python scripts/serve_report.py          # 前台运行
  python scripts/serve_report.py --daemon # 后台运行（Windows 无窗口）
  python scripts/serve_report.py --install # 注册为 Windows 计划任务（开机自启）
  python scripts/serve_report.py --uninstall # 卸载计划任务
  python scripts/serve_report.py --status  # 检查服务是否在运行

接口:
  GET  /                → 最新日报 HTML
  GET  /refresh         → 异步触发管道重跑
  POST /api/report/sync → 接收管道推送的日报（兼容 _sync_to_server）
  GET  /api/status      → 运行状态 JSON

无需安装 Flask，只用 Python 标准库。
"""

import sys
from pathlib import Path

# 确保项目根目录在路径中
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import json
import signal
import subprocess
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from core.logger import get_logger

logger = get_logger("serve_report")

HOST = "localhost"
PORT = 8765
from config.paths import OUTPUT_DIR  # noqa: E402
OBSIDIAN_DIR = Path("D:/Obsidian/SecondBrain/000-Projects/05-量化系统")
LAST_RUN_FILE = OUTPUT_DIR / "reports/last_run.json"

# 运行状态
_running = False
_last_run_time: str | None = None
_last_run_success: bool | None = None
_lock = threading.Lock()
# 当前管道子进程（用于 Ctrl+C 时清理孤儿进程）
_current_proc: subprocess.Popen | None = None
_current_proc_lock = threading.Lock()


def _get_latest_report() -> tuple[str, str | None]:
    """获取最新日报 HTML 和日期。"""
    if not OBSIDIAN_DIR.exists():
        return _fallback_page("日报目录不存在"), None

    html_files = sorted(OBSIDIAN_DIR.glob("量化日报_*.html"), reverse=True)
    if not html_files:
        return _fallback_page("暂无日报"), None

    latest = html_files[0]
    date_str = latest.stem.replace("量化日报_", "")
    html = latest.read_text(encoding="utf-8")
    return html, date_str


def _fallback_page(msg: str) -> str:
    """服务状态页（无日报时显示）。"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>量化日报服务</title>
<style>
body {{ font-family: sans-serif; max-width: 600px; margin: 80px auto; text-align: center; }}
h1 {{ color: #2563eb; }}
.status {{ color: #6b7280; margin: 20px 0; }}
.btn {{ display:inline-block; padding:10px 24px; background:#2563eb; color:#fff;
        text-decoration:none; border-radius:6px; font-size:14px; cursor:pointer; border:none; }}
.btn:hover {{ background:#1d4ed8; }}
.btn:disabled {{ background:#9ca3af; cursor:not-allowed; }}
</style></head><body>
<h1>📊 量化日报服务</h1>
<p class="status">{msg}</p>
<p class="status">服务运行中 | 端口 {PORT}</p>
<button class="btn" onclick="refresh()">🔄 刷新日报</button>
<p id="tip" style="color:#6b7280;font-size:13px;margin-top:12px;"></p>
<script>
async function refresh() {{
    let btn = document.querySelector('.btn');
    let tip = document.getElementById('tip');
    btn.disabled = true; tip.textContent = '正在刷新...';
    try {{
        let r = await fetch('/refresh');
        let d = await r.json();
        if (d.status === 'started') {{
            tip.textContent = '✅ 管道已启动，约 5 分钟完成，请稍后刷新页面';
            setTimeout(() => {{ location.reload(); }}, 120000);
        }} else {{ tip.textContent = '❌ ' + (d.reason || '启动失败'); }}
    }} catch(e) {{ tip.textContent = '❌ 连接服务失败: ' + e.message; }}
    btn.disabled = false;
}}
</script>
</body></html>"""


class ReportHandler(BaseHTTPRequestHandler):
    """日报 HTTP 请求处理器。"""

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            html, date_str = _get_latest_report()
            self._send_html(html)

        elif path == "/refresh":
            self._handle_refresh()

        elif path == "/api/status":
            with _lock:
                status = {
                    "server": "running",
                    "last_run_time": _last_run_time,
                    "last_run_success": _last_run_success,
                    "is_running": _running,
                    "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            # 补充 last_run.json 中的信息
            if LAST_RUN_FILE.exists():
                try:
                    lr = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
                    status["pipeline_full_last_date"] = lr.get("full") or lr.get("last_date")
                    status["pipeline_intraday_last_date"] = lr.get("intraday")
                except Exception:
                    pass
            self._send_json(status)

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        global _last_run_time, _last_run_success
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/report/sync":
            # 接收管道推送（兼容 _sync_to_server）
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body)
                date = data.get("date", "")
                logger.info(f"日报同步请求: {date}, {len(data.get('full_html', ''))} chars")
                with _lock:
                    _last_run_time = date
                    _last_run_success = True
                self._send_json({"status": "ok", "detail": f"日报 {date} 已接收"})
            except Exception as e:
                logger.warning(f"日报同步失败: {e}")
                self._send_json({"status": "error", "detail": str(e)}, 400)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_refresh(self):
        """异步触发管道重跑。"""
        global _running, _last_run_time, _last_run_success
        with _lock:
            if _running:
                self._send_json({"status": "busy", "reason": "管道正在运行中"})
                return
            _running = True

        def _run():
            global _running, _last_run_time, _last_run_success, _current_proc
            try:
                python = sys.executable
                logger.info("触发管道重跑...")
                proc = subprocess.Popen(
                    [python, "-m", "scripts.run_pipeline"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    cwd=str(ROOT),
                )
                with _current_proc_lock:
                    _current_proc = proc
                # 动态超时：轮询子进程 + 检查 pipeline.lock 心跳
                _pipeline_lock = ROOT / "outputs/.pipeline.lock"
                _deadline = time.time() + 3600
                _heartbeat_timeout = 900
                success = False
                while time.time() < _deadline:
                    try:
                        _out, _err = proc.communicate(timeout=30)
                        success = proc.returncode == 0
                        stdout = _out or ""
                        stderr = _err or ""
                        break
                    except subprocess.TimeoutExpired:
                        if _pipeline_lock.exists():
                            try:
                                _mtime = _pipeline_lock.stat().st_mtime
                                if time.time() - _mtime > _heartbeat_timeout:
                                    logger.warning(f"管道无心跳 >{_heartbeat_timeout//60}min，终止")
                                    proc.kill()
                                    _, _err = proc.communicate()
                                    stderr = _err or ""
                                    success = False
                                    break
                            except OSError:
                                pass
                        continue
                else:
                    logger.warning("管道超时（>60min），终止")
                    proc.kill()
                    _, _err = proc.communicate()
                    stderr = _err or ""
                    success = False
                # 失败时写 crash_report
                if not success and stderr:
                    try:
                        _crash = ROOT / "outputs/reports/crash_report.txt"
                        _tail = "\n".join(stderr.strip().split("\n")[-50:])
                        _crash.write_text(
                            f"Time: {datetime.now()}\n"
                            f"Exit: {proc.returncode}\n"
                            f"--- Last 50 lines of stderr ---\n{_tail}",
                            encoding="utf-8",
                        )
                    except Exception:
                        pass
                with _lock:
                    _last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _last_run_success = success
                if success:
                    logger.info("管道重跑完成")
                else:
                    logger.warning(f"管道重跑失败 (exit={proc.returncode})")
                    for line in stderr.split("\n")[-5:]:
                        if line.strip():
                            logger.warning(f"  {line.strip()}")
            except Exception as e:
                with _lock:
                    _last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _last_run_success = False
                logger.warning(f"管道重跑异常: {e}")
            finally:
                with _current_proc_lock:
                    _current_proc = None
                with _lock:
                    _running = False

        threading.Thread(target=_run, daemon=True).start()
        self._send_json({"status": "started", "detail": "管道已异步启动"})

    def log_message(self, format: str, *args):
        logger.debug(f"HTTP {args[0]} {args[1]} {args[2]}")


def _signal_handler(signum, frame):
    """信号处理器 — 收到退出信号时清理管道子进程，然后退出。"""
    with _current_proc_lock:
        proc = _current_proc
    if proc and proc.poll() is None:
        logger.warning(f"收到信号 {signum}，正在终止管道子进程 PID={proc.pid}")
        proc.kill()
        logger.info("管道子进程已终止")
    # 重新抛出 KeyboardInterrupt 以停止 serve_forever
    raise KeyboardInterrupt()


def start_server():
    """启动 HTTP 服务（前台阻塞）。"""
    # 注册信号处理器（仅 SIGINT，Windows 上 SIGTERM 无法从外部 kill 触发）
    signal.signal(signal.SIGINT, _signal_handler)
    server = HTTPServer((HOST, PORT), ReportHandler)
    logger.info(f"日报服务启动: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止")
        server.server_close()


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(description="量化日报 HTTP 服务")
    parser.add_argument("--daemon", action="store_true", help="后台运行（不推荐，用 vbs 替代）")
    parser.add_argument("--daemon-hidden", action="store_true", help="后台隐藏运行（内部使用）")
    parser.add_argument("--install", action="store_true", help="注册为 Windows 计划任务（开机自启）")
    parser.add_argument("--uninstall", action="store_true", help="卸载计划任务")
    parser.add_argument("--status", action="store_true", help="检查服务是否在运行")
    args = parser.parse_args()

    # --status: 检查服务是否在运行
    if args.status:
        import urllib.request
        try:
            resp = urllib.request.urlopen(f"http://{HOST}:{PORT}/api/status", timeout=3)
            data = json.loads(resp.read().decode())
            print(json.dumps(data, ensure_ascii=False, indent=2))
            sys.exit(0)
        except Exception:
            print("服务未运行")
            sys.exit(1)

    # --install: 注册开机自启（写入启动文件夹 + VBS 无窗口）
    if args.install:
        import os

        python_path = sys.executable
        script_path = str(Path(__file__).resolve())
        # 启动文件夹路径
        startup_dir = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        vbs_path = os.path.join(startup_dir, "QuantReportServer.vbs")

        # 生成 VBS 脚本（隐藏窗口启动 Python）
        # 每个路径单独用 Chr(34) 包围，避免嵌套引号导致 CreateProcess 解析为空可执行文件
        vbs_content = (
            'Dim shell\n'
            'Set shell = CreateObject("WScript.Shell")\n'
            'shell.Run Chr(34) & "{}" & Chr(34) & " " & Chr(34) & "{}" & Chr(34), 0, False\n'.format(
                python_path, script_path
            )
        )
        try:
            with open(vbs_path, "w", encoding="utf-8") as f:
                f.write(vbs_content)
            print("[OK] 已注册开机自启: {}".format(vbs_path))
            print("   Python: {}".format(python_path))
            print("   脚本: {}".format(script_path))
            print("   访问: http://{}:{}".format(HOST, PORT))
        except Exception as e:
            print("[FAIL] 写入启动文件夹失败: {}".format(e))
            sys.exit(1)
        sys.exit(0)

    # --uninstall: 卸载开机自启
    if args.uninstall:
        import os
        vbs_path = os.path.join(
            os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
            "QuantReportServer.vbs",
        )
        try:
            os.remove(vbs_path)
            print("[OK] 已卸载开机自启")
        except FileNotFoundError:
            print("[OK] 启动文件不存在，可能已卸载")
        sys.exit(0)

    if args.daemon or args.daemon_hidden:
        # 后台模式：直接启动（不 fork，避免 DETACHED_PROCESS 在 Windows 上的怪异行为）
        # 从外部用 `start /B python serve_report.py` 或 VBS 启动即可
        start_server()

    start_server()
