"""东财统一 HTTP 请求入口 — raw socket + SSL，规避 Python 3.14 TLS 互操作问题。

用法:
    from shared.em_http import em_get
    data = em_get("/api/qt/stock/kline/get", {"secid": "1.600519"})
"""

import gzip
import json
import socket
import ssl
import time
import urllib.parse
from typing import Any

_EM_PORT = 443
_CONNECT_TIMEOUT = 15
_RECV_DEADLINE = 30
_MIN_INTERVAL = 1.0

_last_request = 0.0


def _wait():
    global _last_request
    now = time.time()
    elapsed = now - _last_request
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request = time.time()


def em_get(path: str, params: dict[str, str] | None = None,
           host: str = "push2.eastmoney.com") -> dict[str, Any]:
    """东财统一 GET 请求。使用 raw socket + SSL 绕过 Python 3.14 TLS 问题。

    Fix S: 所有路径确保 sock/ssock 关闭，防止 socket 泄漏。
    """
    _wait()
    query = urllib.parse.urlencode(params or {})
    url = f"{path}?{query}" if query else path

    sock = None
    ssock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT)
        sock.connect((host, _EM_PORT))
        ctx = ssl.create_default_context()
        try:
            ssock = ctx.wrap_socket(sock, server_hostname=host)
        except Exception:
            # SSL 握手失败时 sock 仍未关闭，交给 finally 清理
            raise

        req = f"GET {url} HTTP/1.1\r\nHost: {host}\r\n"
        req += "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36\r\n"
        req += "Referer: https://quote.eastmoney.com/\r\n"
        req += "Accept: */*\r\n"
        req += "Connection: close\r\n\r\n"
        ssock.sendall(req.encode())

        deadline = time.time() + _RECV_DEADLINE
        resp = b""
        while time.time() < deadline:
            try:
                chunk = ssock.recv(65536)
                if not chunk:
                    break
                resp += chunk
                if b"\r\n\r\n" in resp:
                    break
            except socket.timeout:
                break
        ssock.close()
        ssock = None

        he = resp.find(b"\r\n\r\n")
        if he < 0:
            raise ConnectionError(f"{host}: no HTTP header")

        header_raw = resp[:he].decode("utf-8", errors="replace")
        body = resp[he + 4:]
        lines = header_raw.split("\r\n")
        status = int(lines[0].split(" ", 2)[1]) if len(lines[0].split(" ", 2)) >= 2 else 0
        if status != 200:
            raise ConnectionError(f"HTTP {status}: {url[:60]}")

        resp_headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                resp_headers[k.strip().lower()] = v.strip()

        raw = body
        cl = resp_headers.get("content-length", "")
        if cl.isdigit():
            expected = int(cl)
            while len(raw) < expected and time.time() < deadline:
                try:
                    chunk = ssock.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                except socket.timeout:
                    break

        if resp_headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", errors="ignore"))
    finally:
        if ssock is not None:
            try:
                ssock.close()
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
