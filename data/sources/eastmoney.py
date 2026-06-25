"""东方财富数据源 — 直连HTTP，不依赖akshare，作为主力备选

增强说明（2026-06-23）:
  - 添加 ut 伪装 Token，降低 WAF 裸请求拦截概率
  - 添加 _ 时间戳参数，防止缓存
  - 补充完整 AJAX 请求头（Accept, Accept-Language, X-Requested-With 等）
  - 支持 gzip 解压（兼容服务器返回压缩内容的情况）
  - 用 urllib.parse.urlencode 构建参数，比 f-string 更规范
  - 显式列名解析 K 线，比位置索引更可读
  - @retry 装饰器保持不变，由外层处理退避重试和健康追踪
  - 返回值格式保持不变（date 为 index），与接口契约一致

TLS 互操作补丁（2026-06-23）：
  Python 3.14 内置的 urllib.request（底层 http.client.HTTPSConnection）与 EastMoney
  push2his.eastmoney.com 的 IIS/10.0 服务器之间存在 TLS 握手兼容性问题：
    - urllib.request.urlopen() 在 SSL 握手阶段被服务器 RemoteDisconnected
    - requests + urllib3 2.7.0 同样失败（ALPN/TLS 扩展协商不兼容）
    - 原始 socket + ssl.SSLContext.wrap_socket() 可以正常完成握手并获取数据
  因此 _fetch_kline 绕过 Python HTTP 客户端库，直接使用 socket + ssl 发送
  HTTP/1.1 GET 请求。当未来 CPython/urllib3 升级修复此互操作问题时，可恢复
  使用 urllib.request（届时删除本注释及 _send_http_get 函数）。

  强制安全约束（独立院士审查 2026-06-23）：
    - ssl.create_default_context() 绝不设置 check_hostname=False
    - 绝不设置 verify_mode=CERT_NONE
    - 用 Content-Length 精确控制 body 读取范围，不依赖 Connection: close
    - recv 循环有总超时守卫（deadline），防止网络分区导致永久阻塞
"""

import gzip
import json
import socket
import ssl
import time
import urllib.parse

import pandas as pd

from data.interfaces import DataSource
from shared.retry import retry, health_tracker
from core.logger import get_logger

logger = get_logger("data.eastmoney")

# ── 网络参数 ──
_EM_HOST = "push2his.eastmoney.com"
_EM_PORT = 443
_CONNECT_TIMEOUT = 15       # TCP + SSL 握手超时（秒）
_RECV_TOTAL_DEADLINE = 30   # recv 循环总超时（秒），覆盖网络分区场景

# push2his K 线返回字段顺序（11个）
_KLINE_COLUMNS = [
    "date", "open", "close", "high", "low",
    "volume", "amount", "amplitude", "pct_chg", "chg", "turnover",
]
# 输出列
_OUTPUT_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]


def _em_market_code(symbol: str) -> str:
    """股票代码 → 东方财富市场代码 (0=深圳, 1=上海) + secid"""
    code = symbol
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            code = code[2:]
            break
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"1.{code}"
    return f"0.{code}"


def _em_index_code(code: str) -> str:
    """指数代码 → 东方财富 secid"""
    clean = code
    for p in ("sh", "sz", "bj"):
        if clean.startswith(p):
            clean = clean[2:]
            break
    if code.startswith("sh") or clean.startswith("000"):
        return f"1.{clean}"
    return f"0.{clean}"


def _send_http_get(
    host: str,
    path: str,
    headers: dict[str, str],
    connect_timeout: int = _CONNECT_TIMEOUT,
    recv_deadline: int = _RECV_TOTAL_DEADLINE,
) -> tuple[int, dict[str, str], bytes]:
    """用 raw socket + SSL 发送 HTTP/1.1 GET 请求，返回 (status_code, headers, body)。

    为什么不用 urllib.request / requests：
      Python 3.14 + EastMoney IIS/10.0 之间存在 TLS 握手互操作问题，
      urllib.request.urlopen() 在 SSL 握手阶段被 RemoteDisconnected。
      原始 socket + ssl.SSLContext.wrap_socket() 可以正常完成握手。

    安全约束：
      - SSL 证书验证和主机名验证全部开启（create_default_context 默认行为）
      - body 读取基于 Content-Length 精确截断，超时有 deadline 守卫

    Args:
        host:             服务器主机名（用于 TLS SNI 和 HTTP Host 头）
        path:             请求路径，含 query string，如 "/api/...?secid=..."
        headers:          HTTP 请求头（不含 Host，函数自动添加）
        connect_timeout:  TCP 连接 + SSL 握手超时
        recv_deadline:    recv 循环总超时上限

    Returns:
        (status_code, response_headers_dict, body_bytes)

    Raises:
        socket.timeout:  连接/读取超时
        ssl.SSLError:    SSL 握手或证书验证失败
        ConnectionError: 连接被拒绝或重置
    """
    # 1. TCP 连接
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout)
    sock.connect((host, _EM_PORT))

    # 2. SSL 握手 — 严格验证证书 + 主机名
    ctx = ssl.create_default_context()
    ssock = ctx.wrap_socket(sock, server_hostname=host)

    # 3. 构造 HTTP/1.1 请求行 + 头
    request_lines = [f"GET {path} HTTP/1.1", f"Host: {host}"]
    for k, v in headers.items():
        request_lines.append(f"{k}: {v}")
    request_lines.append("Connection: close")
    request_lines.append("")  # 空行结束头部

    raw_request = "\r\n".join(request_lines).encode("utf-8")
    ssock.sendall(raw_request)

    # 4. 接收响应 — 含总超时守卫
    deadline = time.time() + recv_deadline
    response = b""
    header_end_pos = -1

    while header_end_pos == -1 and time.time() < deadline:
        try:
            chunk = ssock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        response += chunk
        # 查找 HTTP 头部结束标记
        header_end_pos = response.find(b"\r\n\r\n")

    if header_end_pos == -1:
        ssock.close()
        raise ConnectionError(f"{host}: HTTP 响应头不完整（已收 {len(response)} 字节）")

    # 5. 解析 HTTP 状态行和响应头
    header_raw = response[:header_end_pos].decode("utf-8", errors="replace")
    header_lines = header_raw.split("\r\n")
    status_line = header_lines[0]
    status_parts = status_line.split(" ", 2)
    status_code = int(status_parts[1]) if len(status_parts) >= 2 else 0

    resp_headers: dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            resp_headers[key.strip().lower()] = value.strip()

    # 6. 按 Content-Length 精确读取 body
    body_prefix = response[header_end_pos + 4:]  # 已接收的 body 部分
    content_length_str = resp_headers.get("content-length", "")
    if content_length_str.isdigit():
        expected_len = int(content_length_str)
        full_body = body_prefix
        while len(full_body) < expected_len and time.time() < deadline:
            remain = expected_len - len(full_body)
            try:
                chunk = ssock.recv(max(4096, remain))
            except socket.timeout:
                break
            if not chunk:
                break
            full_body += chunk
        ssock.close()
        if len(full_body) < expected_len:
            raise ConnectionError(
                f"{host}: body 不完整（期望 {expected_len}，实收 {len(full_body)} 字节）"
            )
        return status_code, resp_headers, full_body
    else:
        # 无 Content-Length 时读到 socket close
        full_body = body_prefix
        while time.time() < deadline:
            try:
                chunk = ssock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            full_body += chunk
        ssock.close()
        return status_code, resp_headers, full_body


class EastMoneyDataSource(DataSource):
    """东方财富直连数据源 — 免费、无需token"""

    ENDPOINT = "eastmoney_price"

    def __init__(self):
        from shared.anticrawl import AntiCrawlGuard
        self.guard = AntiCrawlGuard("eastmoney")

    def name(self) -> str:
        return "eastmoney"

    def is_available(self) -> bool:
        return health_tracker.is_available(self.ENDPOINT)

    def _fetch_kline(self, secid: str, start: str, end: str, klt: int = 101) -> pd.DataFrame:
        """通用K线获取 — 直连东方财富 push2his API。"""
        # 反爬等待
        self.guard.wait()

        # 1. 构造请求参数
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": str(klt),
            "fqt": "1",
            "beg": start.replace("-", ""),
            "end": end.replace("-", ""),
            "lmt": "5000",
            "_": str(int(time.time() * 1000)),
        }
        path = f"/api/qt/stock/kline/get?{urllib.parse.urlencode(params)}"

        # 使用守卫生成的完整浏览器请求头
        request_headers = self.guard.get_headers(
            custom_referer="https://quote.eastmoney.com/"
        )
        # EastMoney 需要 X-Requested-With 标记 AJAX 请求
        request_headers["X-Requested-With"] = "XMLHttpRequest"

        # 2. 发送 HTTP/1.1 GET（raw socket + SSL，绕过 urllib TLS 互操作问题）
        status_code, resp_headers, raw_body = _send_http_get(
            _EM_HOST, path, request_headers,
        )

        if status_code != 200:
            logger.warning(
                f"eastmoney _fetch_kline HTTP {status_code}: secid={secid}"
            )
            return pd.DataFrame()

        # 3. 解压 / 解码
        if resp_headers.get("content-encoding") == "gzip":
            raw_body = gzip.decompress(raw_body)
        text = raw_body.decode("utf-8", errors="ignore")

        # 4. 解析 JSON → DataFrame
        data = json.loads(text)
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return pd.DataFrame()

        rows = [k.split(",") for k in klines]
        df = pd.DataFrame(rows, columns=_KLINE_COLUMNS)

        numeric_cols = [
            "open", "close", "high", "low", "volume", "amount",
            "amplitude", "pct_chg", "chg", "turnover",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 5. 统一输出格式：date 为 DatetimeIndex
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        return df[_OUTPUT_COLUMNS].set_index("date").sort_index()

    @retry(max_retries=2, base_delay=3.0)
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """获取个股日线（前复权）"""
        try:
            secid = _em_market_code(symbol)
            df = self._fetch_kline(secid, start, end)
            if df.empty:
                return df
            df["symbol"] = symbol
            health_tracker.record_success(self.ENDPOINT)
            return df
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"eastmoney 获取个股 {symbol} 失败: {e}")
            raise

    @retry(max_retries=2, base_delay=1.0)
    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """获取指数日线（前复权）"""
        try:
            secid = _em_index_code(code)
            df = self._fetch_kline(secid, start, end)
            if df.empty:
                return df
            df["symbol"] = code
            if "amount" not in df.columns:
                df["amount"] = 0.0
            health_tracker.record_success(self.ENDPOINT)
            self.guard.on_success()
            return df
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            self.guard.on_failure()
            logger.warning(f"eastmoney 获取指数 {code} 失败: {e}")
            raise
