"""交易日志 — 完整记录每笔交易的参数快照 + 市场环境"""
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.logger import get_logger

logger = get_logger("sniper.engine.trade_log")

TRADE_LOG_DIR = Path(__file__).resolve().parents[2] / "outputs" / "trade_logs"


@dataclass
class TradeLog:
    """单笔完整交易记录。

    记录一笔从买入到卖出的完整交易数据：
    - 股票信息：代码、板块、评分
    - 价格信息：入场/出场价、PnL
    - 市场环境：入场时 L0/L1/L2 评分
    - 参数快照：当时使用的参数（用于参数敏感性分析）
    """
    # 股票信息
    symbol: str
    sector: str = ""
    entry_reason: str = ""        # L3 入场原因

    # 买卖时间
    entry_date: str = ""
    exit_date: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    shares: int = 0
    hold_days: int = 0

    # 收益
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""

    # 入场时市场环境
    l0_score: float = 50.0       # L0 市场评分
    l1_sector_rank: int = 1      # 板块排名
    l2_score: float = 0.0        # L2 个股评分

    # 参数快照（用于参数敏感性分析）
    params_snapshot: str = ""     # JSON string of params at trade time

    # 元数据
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def params(self) -> dict:
        """解析参数快照。"""
        if not self.params_snapshot:
            return {}
        try:
            return json.loads(self.params_snapshot)
        except (json.JSONDecodeError, TypeError):
            return {}


class TradeLogStore:
    """交易日志持久化存储。写入 parquet 文件，按月分片。"""

    def __init__(self, base_dir: str | Path | None = None):
        self.base_dir = Path(base_dir or TRADE_LOG_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _month_file(self, year_month: str) -> Path:
        return self.base_dir / f"trades_{year_month}.parquet"

    def append(self, trade: TradeLog | dict):
        """追加一笔交易到对应月份的 parquet。"""
        if isinstance(trade, TradeLog):
            record = trade.to_dict()
        else:
            record = trade

        entry_date = record.get("entry_date", "")
        ym = entry_date[:7] if len(entry_date) >= 7 else "unknown"
        fpath = self._month_file(ym)

        df = pd.DataFrame([record])

        if fpath.exists():
            try:
                old = pd.read_parquet(fpath)
                df = pd.concat([old, df], ignore_index=True)
            except Exception:
                pass

        df.to_parquet(fpath, index=False)
        logger.debug(f"[TradeLog] 追加: {record.get('symbol')} {entry_date} → {fpath.name}")

    def append_batch(self, trades: list[TradeLog | dict]):
        """批量追加交易。"""
        if not trades:
            return
        # 按月份分组
        by_month: dict[str, list[dict]] = {}
        for t in trades:
            rec = t.to_dict() if isinstance(t, TradeLog) else t
            entry_date = rec.get("entry_date", "")
            ym = entry_date[:7] if len(entry_date) >= 7 else "unknown"
            if ym not in by_month:
                by_month[ym] = []
            by_month[ym].append(rec)

        for ym, recs in by_month.items():
            fpath = self._month_file(ym)
            df = pd.DataFrame(recs)
            if fpath.exists():
                try:
                    old = pd.read_parquet(fpath)
                    df = pd.concat([old, df], ignore_index=True)
                except Exception:
                    pass
            df.to_parquet(fpath, index=False)

        logger.info(f"[TradeLog] 批量追加: {len(trades)} 笔, {len(by_month)} 个月")

    def query(self, start_date: str = "", end_date: str = "",
              symbol: str = "") -> pd.DataFrame:
        """查询交易记录。"""
        all_files = sorted(self.base_dir.glob("trades_*.parquet"))
        if not all_files:
            return pd.DataFrame()

        # 过滤月份范围
        if start_date and end_date:
            start_ym = start_date[:7]
            end_ym = end_date[:7]
            all_files = [f for f in all_files if start_ym <= f.stem.replace("trades_", "") <= end_ym]

        dfs = []
        for f in all_files:
            try:
                df = pd.read_parquet(f)
                dfs.append(df)
            except Exception:
                continue

        if not dfs:
            return pd.DataFrame()

        result = pd.concat(dfs, ignore_index=True)

        # 日期过滤
        if start_date:
            result = result[result["entry_date"] >= start_date]
        if end_date:
            result = result[result["exit_date"] <= end_date]
        if symbol:
            result = result[result["symbol"] == symbol]

        return result.sort_values("entry_date", ascending=False).reset_index(drop=True)

    def stats(self) -> dict:
        """交易数据统计。"""
        all_files = list(self.base_dir.glob("trades_*.parquet"))
        total_trades = 0
        total_files = len(all_files)
        date_range = ""
        for f in all_files:
            try:
                df = pd.read_parquet(f)
                total_trades += len(df)
            except Exception:
                continue

        return {
            "total_trades": total_trades,
            "total_files": total_files,
            "date_range": date_range,
            "db_path": str(self.base_dir),
        }
