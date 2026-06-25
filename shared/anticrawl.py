"""统一动态反爬守卫 — 覆盖所有数据源的反爬与反检测

核心设计：
  - DataSource 无关：守卫只负责时序/头部/TLS，不感知具体源
  - 每源独立上下文：会话内保持 UA/Cookie 一致，更接近真人
  - 交易时段感知：盘中加速（尽快拿数据）、盘后减速（降低服务器压力）
  - 周末/节假日：大幅降速，仅在需要补数据时访问
  - 失败自适应退避：连续失败越多，等待越久（3s → 30s → 90s）
  - 突发控制：连续 N 次请求后强制停顿 5-15s
  - 请求间隔：对数正态分布，复现真人"阅读-操作"节奏

用法:
  from shared.anticrawl import AntiCrawlGuard

  guard = AntiCrawlGuard("eastmoney")
  guard.wait()                        # ⏱ 等待真人操作间隔
  headers = guard.get_headers()       # 📋 获取伪装请求头
  guard.on_success()                  # ✅ 记录成功
"""

import math
import random
import time
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

# ── 100+ 浏览器 UA 池 ──

_USER_AGENTS = [
    # ── Chrome 125 (主力) ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 124
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 123
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome 122
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome 121
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome 120
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 119
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Chrome 118 (Win11)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    # Chrome 109 (旧版，兼容老旧金融终端)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    # Chrome 100
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36",
    # ── Chrome Mac ──
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # ── Edge ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # ── Firefox ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # ── Safari ──
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    # ── Opera ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 OPR/110.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OPR/109.0.0.0",
    # ── 360 安全浏览器 ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.4638.69 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36",
    # ── 搜狗浏览器 ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.5481.178 Safari/537.36 SE 2.X MetaSr 1.0",
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.5359.125 Safari/537.36 SE 2.X MetaSr 1.0",
    # ── QQ 浏览器 ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 QBCore/4.0.1327.1 QQBrowser/12.0.5180.400",
    # ── Chrome Mobile (Android) ──
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36 EdgA/125.0.0.0",
    # ── Firefox Mobile (Android) ──
    "Mozilla/5.0 (Android 14; Mobile; rv:126.0) Gecko/126.0 Firefox/126.0",
    "Mozilla/5.0 (Android 13; Mobile; rv:124.0) Gecko/124.0 Firefox/124.0",
    # ── 微信内置浏览器 ──
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/89.0.4389.128 Mobile Safari/537.36 MicroMessenger/8.0.58",
    # ── 同花顺 App 内置 (Android) ──
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0.6099.144 Mobile Safari/537.36 Hexin/13.0",
    # ─── 东方财富 App 内置 ───
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0.6099.144 Mobile Safari/537.36 EastMoney/11.8",
    # ─── Chrome Linux ───
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# 辅助：从 UA 中提取 Chrome 版本号
import re as _re
_UA_CHROME_VER_RE = _re.compile(r'Chrome/(\d+)')

def _extract_chrome_version(ua: str) -> str:
    """从 UA 字符串提取 Chrome/Chromium 版本号。失败返回 '125'。"""
    m = _UA_CHROME_VER_RE.search(ua)
    if m:
        return m.group(1)
    # Firefox 或 Safari
    if "Firefox/" in ua:
        m = _re.search(r'Firefox/(\d+)', ua)
        return m.group(1) if m else "126"
    if "Version/" in ua:
        m = _re.search(r'Version/(\d+)', ua)
        return m.group(1) if m else "17"
    return "125"

def _detect_platform(ua: str) -> str:
    """从 UA 检测操作系统平台。"""
    if "Android" in ua:
        return '"Android"'
    if "iPhone" in ua or "iPad" in ua:
        return '"iOS"'
    if "Mac OS X" in ua or "Macintosh" in ua:
        return '"macOS"'
    if "Linux" in ua and "Android" not in ua:
        return '"Linux"'
    return '"Windows"'

def _detect_is_mobile(ua: str) -> str:
    """从 UA 检测是否为移动端。"""
    return "?1" if ("Mobile" in ua or "MicroMessenger" in ua) else "?0"


@dataclass
class AntiCrawlProfile:
    """数据源反爬档案 — 决定各源的行为特征

    不同数据源有不同的反爬容忍度，通过调整档案参数适配：
    - 证券公司 API（tushare/jqdata）：速度快，不需要太多伪装
    - 财经网站（eastmoney/sina/10jqka）：需要真人模拟
    - 聚合 SDK（akshare）：自带一定反爬，只加时序控制
    """
    name: str
    # ── 请求模式 ──
    request_mode: str = "navigation"  # "ajax" 或 "navigation"
    # ── UA 偏好 ──
    ua_pool_index: tuple[int, int] = (0, len(_USER_AGENTS))  # UA 池切片范围
    default_referer: str = "https://www.baidu.com/"
    extra_headers: dict[str, str] = field(default_factory=dict)

    # ── 时序参数 ──
    base_delay_mean: float = 2.0       # 请求间隔均值(秒)
    base_delay_std: float = 0.8        # 请求间隔标准差
    burst_limit: int = 30              # 连续请求上限
    burst_pause_min: float = 5.0       # 突发停顿最小值
    burst_pause_max: float = 15.0      # 突发停顿最大值

    # ── 交易时段 ──
    trading_hour_speedup: float = 0.6  # 交易时段加速系数 (<1=更快)
    after_hour_slowdown: float = 2.5   # 盘后减速系数
    weekend_slowdown: float = 6.0      # 周末减速系数

    # ── 失败退避 ──
    backoff_base: float = 3.0          # 首次失败等待
    backoff_factor: float = 3.0        # 退避倍数
    backoff_cap: float = 90.0          # 退避上限

    # ── 会话 ──
    session_keepalive: bool = True     # 会话内保持 UA
    session_refresh_interval: int = 200  # 每 N 次请求轮换会话


# ── 预定义数据源反爬档案 ──

SOURCE_PROFILES: dict[str, AntiCrawlProfile] = {
    # 东方财富 — 反爬严格，需要完整浏览器伪装
    "eastmoney": AntiCrawlProfile(
        name="eastmoney",
        request_mode="ajax",
        default_referer="https://quote.eastmoney.com/",
        base_delay_mean=2.5,
        base_delay_std=1.0,
        burst_limit=15,
        burst_pause_min=8.0,
        burst_pause_max=20.0,
        after_hour_slowdown=4.0,
        extra_headers={
            "X-Requested-With": "XMLHttpRequest",
        },
    ),
    # 新浪 — 较宽松
    "sina": AntiCrawlProfile(
        name="sina",
        request_mode="ajax",
        default_referer="https://finance.sina.com.cn/",
        base_delay_mean=1.5,
        base_delay_std=0.5,
        burst_limit=50,
        burst_pause_min=3.0,
        burst_pause_max=8.0,
        extra_headers={
            "Referer": "https://finance.sina.com.cn/",
        },
    ),
    # 同花顺/10jqka — 较严格
    "10jqka": AntiCrawlProfile(
        name="10jqka",
        request_mode="ajax",
        default_referer="https://www.10jqka.com.cn/",
        base_delay_mean=2.0,
        base_delay_std=0.8,
        burst_limit=20,
        burst_pause_min=5.0,
        burst_pause_max=15.0,
        after_hour_slowdown=3.0,
        extra_headers={
            "Referer": "https://www.10jqka.com.cn/",
        },
    ),
    # AKShare（聚合SDK）— 内部自带反爬，只加时序
    "akshare": AntiCrawlProfile(
        name="akshare",
        default_referer="",
        base_delay_mean=0.8,
        base_delay_std=0.3,
        burst_limit=100,
        burst_pause_min=2.0,
        burst_pause_max=5.0,
        trading_hour_speedup=0.5,
    ),
    # AKShare Daily（新浪源）
    "akshare_daily": AntiCrawlProfile(
        name="akshare_daily",
        default_referer="",
        base_delay_mean=1.0,
        base_delay_std=0.4,
        burst_limit=80,
        burst_pause_min=2.0,
        burst_pause_max=5.0,
    ),
    # Tushare — SDK 自带 token 认证，较友好
    "tushare": AntiCrawlProfile(
        name="tushare",
        default_referer="",
        base_delay_mean=0.3,
        base_delay_std=0.1,
        burst_limit=200,
        burst_pause_min=1.0,
        burst_pause_max=3.0,
        trading_hour_speedup=0.3,
    ),
    # JQData（聚宽）— SDK 自带认证
    "jqdata": AntiCrawlProfile(
        name="jqdata",
        default_referer="",
        base_delay_mean=0.2,
        base_delay_std=0.1,
        burst_limit=500,
        burst_pause_min=0.5,
        burst_pause_max=2.0,
        trading_hour_speedup=0.2,
    ),
    # Mootdx（通达信直连）— TCP 直连，不需要 HTTP 伪装
    "mootdx": AntiCrawlProfile(
        name="mootdx",
        default_referer="",
        base_delay_mean=0.5,
        base_delay_std=0.2,
        burst_limit=200,
        burst_pause_min=1.0,
        burst_pause_max=3.0,
    ),
    # Baostock — 已弃用（IP 黑名单），档案保留但不会被使用
    "baostock": AntiCrawlProfile(
        name="baostock",
        default_referer="",
        base_delay_mean=10.0,
        base_delay_std=3.0,
        burst_limit=5,
        burst_pause_min=30.0,
        burst_pause_max=60.0,
    ),
}


_NOW = time.time


def _is_trading_hours() -> bool:
    """判断当前是否为 A 股交易时段（9:30-15:00）"""
    now = time.localtime()
    h, m = now.tm_hour, now.tm_min
    return (h == 9 and m >= 30) or (10 <= h <= 14) or (h == 15 and m == 0)


def _is_weekend() -> bool:
    """判断当前是否为周末"""
    return time.localtime().tm_wday >= 5


def _speed_factor(profile: AntiCrawlProfile) -> float:
    """根据时间返回当前速度系数（<1=加速, >1=减速）"""
    if _is_weekend():
        return profile.weekend_slowdown
    if _is_trading_hours():
        return profile.trading_hour_speedup
    return profile.after_hour_slowdown


def _build_sec_ch_ua(ua: str) -> str:
    """从 UA 版本号动态构造 Sec-CH-UA，确保版本与 UA 一致。"""
    ver = _extract_chrome_version(ua)
    if "Chrome" in ua:
        if "Edg" in ua:
            return f'"Not A(Brand";v="99", "Microsoft Edge";v="{ver}", "Chromium";v="{ver}"'
        if "OPR" in ua or "Opera" in ua:
            return f'"Not A(Brand";v="99", "Opera";v="{ver}", "Chromium";v="{ver}"'
        return f'"Not A(Brand";v="99", "Google Chrome";v="{ver}", "Chromium";v="{ver}"'
    if "Firefox" in ua:
        return f'"Not A(Brand";v="99", "Firefox";v="{ver}", "Mozilla";v="{ver}"'
    if "Safari" in ua:
        return f'"Not A(Brand";v="99", "Safari";v="{ver}", "WebKit";v="{ver}"'
    return f'"Not A(Brand";v="99", "Google Chrome";v="{ver}", "Chromium";v="{ver}"'


class AntiCrawlGuard:
    """统一动态反爬守卫 — 每个数据源独立上下文

    提供三个核心方法：
      guard.wait()          → 根据时间/状态等待真人间隔
      guard.get_headers()   → 返回完整浏览器请求头
      guard.on_success/on_failure → 记录健康状态
    """

    def __init__(self, source_name: str):
        if source_name not in SOURCE_PROFILES:
            raise KeyError(f"未知数据源: {source_name}，可用: {list(SOURCE_PROFILES.keys())}")
        self.profile = SOURCE_PROFILES[source_name]
        self.source_name = source_name

        # ── 会话状态 ──
        self.session_ua: str = ""
        self.session_id: str = ""
        self._rotate_session()

        # ── 请求计数 ──
        self.request_count = 0
        self.burst_count = 0
        self.burst_reset_time = _NOW()

        # ── 失败退避 ──
        self.consecutive_failures = 0
        self.last_fail_time = 0.0

        # ── Cookie Jar ──
        from http.cookiejar import CookieJar
        self._cookie_jar = CookieJar()

        # ── 上次请求时间（用于确保间隔） ──
        self.last_request_time = 0.0

        # ── 线程锁 ──
        self._lock = threading.Lock()

    def _rotate_session(self):
        """轮换会话：选新 UA + 新 Session ID"""
        lo, hi = self.profile.ua_pool_index
        self.session_ua = random.choice(_USER_AGENTS[lo:hi])
        self.session_id = uuid4().hex[:8]
        # 随机会话附带随机初始延迟档位
        self._base_delay_jitter = random.uniform(0.8, 1.2)
        self.burst_count = 0
        self.burst_reset_time = _NOW()

    def wait(self, forced_delay: float | None = None):
        """等待 — 模拟真人操作间隔

        时序模型（对数正态分布）复现真人的操作节奏：
        - 偶尔快速连续操作（小概率 <0.5s）
        - 偶尔停顿思考（大概率 2-8s）
        - 触发突发控制时强制长停顿 5-15s

        Args:
            forced_delay: 强制指定等待秒数（覆盖自动计算）
        """
        with self._lock:
            now = _NOW()

            if forced_delay is not None:
                delay = forced_delay
            else:
                # 1) 基础延迟：对数正态分布
                mu = math.log(
                    self.profile.base_delay_mean**2
                    / math.sqrt(self.profile.base_delay_std**2 + self.profile.base_delay_mean**2)
                )
                sigma = math.sqrt(
                    math.log(1 + self.profile.base_delay_std**2 / self.profile.base_delay_mean**2)
                )
                delay = max(0.2, float(random.lognormvariate(mu, sigma)))
                delay *= self._base_delay_jitter

                # 2) 时段系数
                delay *= _speed_factor(self.profile)

                # 3) 失败退避
                if self.consecutive_failures > 0:
                    b = self.profile.backoff_base * (self.profile.backoff_factor ** (self.consecutive_failures - 1))
                    b = min(b, self.profile.backoff_cap)
                    # 退避中注入随机抖动 [-30%, +30%]
                    b *= random.uniform(0.7, 1.3)
                    delay += b

                # 4) 突发控制
                # 每分钟重置突发计数
                if now - self.burst_reset_time > 60:
                    self.burst_count = 0
                    self.burst_reset_time = now

                self.request_count += 1
                self.burst_count += 1

                if self.burst_count >= self.profile.burst_limit:
                    burst_pause = random.uniform(
                        self.profile.burst_pause_min,
                        self.profile.burst_pause_max,
                    )
                    delay += burst_pause
                    self.burst_count = 0

                # 5) 每轮换间隔轮换会话
                if self.profile.session_keepalive and self.request_count % self.profile.session_refresh_interval == 0:
                    self._rotate_session()

            # 确保自上次请求以来的间隔足够
            elapsed = now - self.last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)

            self.last_request_time = _NOW()

    def get_headers(self, custom_referer: str | None = None) -> dict[str, str]:
        """生成真人浏览器的完整请求头

        根据 request_mode 自动切换：
        - AJAX 模式（数据API）：application/json Accept, 无导航头
        - 导航模式（网页）：text/html Accept, 完整 Sec-Fetch-* 导航头

        Args:
            custom_referer: 自定义 Referer，默认用档案配置

        Returns:
            伪装请求头字典
        """
        referer = custom_referer or self.profile.default_referer
        is_ajax = self.profile.request_mode == "ajax"
        platform = _detect_platform(self.session_ua)
        is_mobile = _detect_is_mobile(self.session_ua)

        # 公共头（所有模式）
        headers: dict[str, str] = {
            "User-Agent": self.session_ua,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-CH-UA": _build_sec_ch_ua(self.session_ua),
            "Sec-CH-UA-Mobile": is_mobile,
            "Sec-CH-UA-Platform": platform,
        }

        # 根据请求模式选择 Accept 头和导航头
        if is_ajax:
            # AJAX/XHR 模式：数据接口（同花顺、东方财富等）
            headers["Accept"] = "application/json, text/plain, */*; q=0.01"
            headers["Sec-Fetch-Dest"] = "empty"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Site"] = "same-site"
        else:
            # 导航模式：页面浏览
            headers["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            )
            headers["Sec-Fetch-Dest"] = "document"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-Site"] = "none"
            headers["Sec-Fetch-User"] = "?1"
            headers["Upgrade-Insecure-Requests"] = "1"

        # 合并源特有的额外头
        headers.update(self.profile.extra_headers)

        # 只在有 Referer 时添加
        if referer:
            headers["Referer"] = referer
            if "://" in referer:
                origin = "/".join(referer.split("/")[:3])
                headers["Origin"] = origin

        # Cookie（使用 CookieJar 的导出）
        cookie_header = self._cookie_jar_output()
        if cookie_header:
            headers["Cookie"] = cookie_header

        return headers

    def update_cookies(self, response_headers: dict[str, str], request_url: str | None = None):
        """从 HTTP 响应头更新 CookieJar

        Args:
            response_headers: HTTP 响应头字典
            request_url: 请求 URL（用于 Cookie 作用域判断）
        """
        set_cookie = response_headers.get("Set-Cookie") or response_headers.get("set-cookie")
        if not set_cookie:
            return
        try:
            from http.cookiejar import Cookie
            from urllib.parse import urlparse
            parsed = urlparse(request_url) if request_url else None
            # 逐条解析 Set-Cookie
            for raw_cookie in set_cookie.split("\n") if "\n" in set_cookie else [set_cookie]:
                raw_cookie = raw_cookie.strip()
                if not raw_cookie:
                    continue
                # 分离 cookie name=value 和属性
                parts = raw_cookie.split(";")
                first = parts[0].strip()
                if "=" not in first:
                    continue
                name, value = first.split("=", 1)
                from http.cookiejar import Cookie
                c = Cookie(
                    version=0,
                    name=name.strip(),
                    value=value.strip(),
                    port=None,
                    port_specified=False,
                    domain=parsed.hostname if parsed else "",
                    domain_specified=bool(parsed),
                    domain_initial_dot=False,
                    path=parsed.path if parsed else "/",
                    path_specified=True,
                    secure="secure" in raw_cookie.lower(),
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
                self._cookie_jar.set_cookie(c)
        except Exception:
            pass  # Cookie 解析失败不阻塞

    def _cookie_jar_output(self) -> str:
        """从 CookieJar 导出 Cookie 请求头字符串"""
        try:
            from http.cookiejar import CookieJar
            # 简单输出所有非过期 cookie
            cookies = []
            now = time.time()
            for c in self._cookie_jar:
                if c.expires is not None and c.expires < now:
                    continue
                cookies.append(f"{c.name}={c.value}")
            return "; ".join(cookies) if cookies else ""
        except Exception:
            return ""

    def on_success(self):
        """记录成功 — 重置失败计数"""
        with self._lock:
            self.consecutive_failures = 0

    def on_failure(self):
        """记录失败 — 递增退避计数"""
        with self._lock:
            self.consecutive_failures += 1
            self.last_fail_time = _NOW()

    @property
    def current_backoff(self) -> float:
        """当前退避等待秒数（评估用）"""
        if self.consecutive_failures == 0:
            return 0.0
        b = self.profile.backoff_base * (self.profile.backoff_factor ** (self.consecutive_failures - 1))
        return min(b, self.profile.backoff_cap)

    def reset(self):
        """完全重置守卫状态"""
        with self._lock:
            self._rotate_session()
            self.request_count = 0
            self.burst_count = 0
            self.burst_reset_time = _NOW()
            self.consecutive_failures = 0
            self.last_fail_time = 0.0
            self.last_request_time = 0.0
            from http.cookiejar import CookieJar
            self._cookie_jar = CookieJar()
