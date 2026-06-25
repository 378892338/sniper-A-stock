"""MootdxRealtimeSource — 通达信 TCP 实时行情（12:00 盘中快照专线）

P0 缺陷修复:
  1. 北交所股票过滤 (P-CRIT-01)
  2. batch_size 可配置 (P-CRIT-02)
  3. volume /100 股->手 (Q-CRIT-04)
  4. 频率控制 <=1次/10秒 (S-CRIT-01)
  5. IP 随机轮换 (S-CRIT-01)
  6. heartbeat=True 保活 (S-CRIT-01)
  7. 保留 active1 停牌检测 (S-CRIT-04)
"""

import random
import time as _time
from datetime import datetime

import pandas as pd

from mootdx.quotes import Quotes
from mootdx.consts import HQ_HOSTS

from core.logger import get_logger

logger = get_logger("data.mootdx.realtime")

# 北交所前缀 — quotes() 不支持
_BJ_PREFIXES = ("8", "920")

# IP 轮换池（从 mootdx.consts.HQ_HOSTS 提取）
_IP_POOL = [
    f"{host[1]}:{host[2]}"
    for host in HQ_HOSTS
    if isinstance(host, (list, tuple)) and len(host) >= 3
]


class MootdxRealtimeSource:
    """通达信 TCP 实时行情 — 仅供 12:00 盘中管道使用

    设计要点:
      - 每次 fetch 建新连接，用完关闭（避免服务端断开）
      - 从 30+ 服务器随机选 IP 防聚集识别
      - 频率控制：min_interval 秒内不重复发送
      - 北交所自动过滤
    """

    ENDPOINT = "mootdx_realtime"
    _BJ_PREFIXES = _BJ_PREFIXES  # 暴露供测试访问

    def __init__(self, batch_size: int = 200, min_interval: float = 10.0,
                 ip_pool: list[str] | None = None, heartbeat: bool = True):
        self.batch_size = batch_size
        self.min_interval = min_interval
        self.ip_pool = ip_pool or _IP_POOL
        self.heartbeat = heartbeat
        self._last_call = 0.0

    def _rate_limit(self):
        elapsed = _time.time() - self._last_call
        if elapsed < self.min_interval:
            _time.sleep(self.min_interval - elapsed)
        self._last_call = _time.time()

    def _pick_server(self) -> tuple[str, int]:
        entry = random.choice(self.ip_pool)
        parts = entry.split(":")
        return (parts[0], int(parts[1]) if len(parts) > 1 else 7709)

    def _connect(self):
        ip, port = self._pick_server()
        logger.debug(f"mootdx connect: {ip}:{port}")
        return Quotes.factory(
            market="std", server=(ip, port), timeout=15,
            heartbeat=self.heartbeat, bestip=False,
        )

    def fetch_quotes(self, symbols: list[str]) -> pd.DataFrame:
        today = datetime.now().strftime("%Y-%m-%d")

        # P-CRIT-01: 过滤北交所
        filtered = [s for s in symbols if not s.startswith(_BJ_PREFIXES)]
        skipped = len(symbols) - len(filtered)
        if skipped:
            logger.info(f"北交所过滤: {skipped} 只跳过")

        if not filtered:
            return pd.DataFrame()

        all_rows = []
        for i in range(0, len(filtered), self.batch_size):
            batch = filtered[i:i + self.batch_size]
            self._rate_limit()
            client = self._connect()
            try:
                result = client.quotes(symbol=batch)
                if result is None or result.empty:
                    logger.warning(f"mootdx batch empty: offset={i}")
                    continue

                for _, row in result.iterrows():
                    vol_hand = row["vol"] / 100.0 if pd.notna(row.get("vol")) else 0.0
                    entry = {
                        "symbol": str(row["code"]).zfill(6),
                        "price": row.get("price"),
                        "last_close": row.get("last_close"),
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "volume": vol_hand,
                        "amount": row.get("amount"),
                        "servertime": str(row.get("servertime", "")),
                        "active1": row.get("active1", 0),
                        "bid1": row.get("bid1"), "ask1": row.get("ask1"),
                        "bid_vol1": row.get("bid_vol1"), "ask_vol1": row.get("ask_vol1"),
                        "bid2": row.get("bid2"), "ask2": row.get("ask2"),
                        "bid3": row.get("bid3"), "ask3": row.get("ask3"),
                        "bid4": row.get("bid4"), "ask4": row.get("ask4"),
                        "bid5": row.get("bid5"), "ask5": row.get("ask5"),
                    }
                    all_rows.append(entry)
            except Exception as e:
                logger.warning(f"mootdx batch fail: {e}")
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)

        # P-CRIT-04: price 与 last_close 交叉检查
        if "price" in df.columns and "last_close" in df.columns:
            mask = df["last_close"].notna() & (df["last_close"] > 0)
            df.loc[mask, "_ratio"] = (
                (df["price"] - df["last_close"]).abs() / df["last_close"]
            )
            bad = df[df["_ratio"] > 0.5].index
            if len(bad) > 0:
                logger.warning(f"防御校验: {len(bad)} 条 price 异常, 已过滤")
                df = df.drop(bad)
            df = df.drop(columns=["_ratio"], errors="ignore")

        return df
