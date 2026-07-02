"""本地数据仓库 — 全量 A 股数据本地化存储与读取

所有数据口径统一从本地 SQLite 获取，避免每次回测都重新拉取。
支持增量更新，设计用于每周自动刷新。
"""

import sqlite3
from pathlib import Path
from datetime import datetime, date

import pandas as pd

from data.local.schema import (
    DB_FILE, ALL_DDLS,
    T_STOCK_LIST, T_TRADE_CAL, T_DAILY_BARS,
    T_INDEX_DAILY, T_SW_LIST, T_SW_DAILY, T_UPDATE_LOG,
    T_VALUATION, T_INTRA_SNAPSHOT,
)
from core.logger import get_logger

logger = get_logger("data.local.warehouse")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class LocalDataWarehouse:
    """本地数据仓库 — 所有数据从 SQLite 读写。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or DB_FILE)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── 连接管理 ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self):
        """初始化数据库表结构（幂等）。"""
        conn = self._connect()
        try:
            for ddl in ALL_DDLS:
                conn.execute(ddl)
            conn.commit()
        finally:
            conn.close()
        logger.info(f"数据仓库就绪: {self.db_path}")

    # ── 股票列表 ──

    def update_stock_list(self, df: pd.DataFrame):
        """全量替换股票列表。df 列: symbol, name (可选 list_date, status)"""
        conn = self._connect()
        try:
            df.to_sql(T_STOCK_LIST, conn, if_exists="replace", index=False)
            conn.commit()
            logger.info(f"股票列表更新: {len(df)} 只")
        finally:
            conn.close()

    def get_stock_list(self, status: str | None = "active") -> pd.DataFrame:
        conn = self._connect()
        try:
            if status:
                sql = f"SELECT * FROM {T_STOCK_LIST} WHERE status = ? ORDER BY symbol"
                return pd.read_sql(sql, conn, params=(status,))
            return pd.read_sql(f"SELECT * FROM {T_STOCK_LIST} ORDER BY symbol", conn)
        finally:
            conn.close()

    # ── 个股日线 ──

    def store_daily_bars(self, df: pd.DataFrame, if_exists: str = "append"):
        """存入个股日线。df 列: symbol, date, open, high, low, close, volume, amount

        不使用 temp table，直接用 DataFrame 列名做 INSERT OR IGNORE，
        避免 ADD COLUMN 后列数不匹配或 temp table 命名空间冲突。
        """
        if df.empty:
            return
        df2 = df.copy()
        # date 可能在 index 中（来自 normalize 输出）→ reset_index 拿回列
        if "date" not in df2.columns and isinstance(df2.index, pd.DatetimeIndex):
            df2 = df2.reset_index()
        # date 列转字符串（统一 YYYY-MM-DD，去掉时间戳后缀）
        if "date" in df2.columns:
            try:
                df2["date"] = pd.to_datetime(df2["date"]).dt.strftime("%Y-%m-%d")
            except Exception:
                df2["date"] = df2["date"].astype(str).str[:10]
        conn = self._connect()
        try:
            conn.execute("BEGIN")  # Fix C: 显式事务防止崩溃时部分写入
            if if_exists == "append":
                cols = ", ".join(f'"{c}"' for c in df2.columns)
                ph = ", ".join("?" for _ in df2.columns)
                data = [tuple(row) for row in df2[df2.columns].to_numpy()]
                conn.executemany(
                    f"INSERT OR IGNORE INTO {T_DAILY_BARS} ({cols}) VALUES ({ph})",
                    data,
                )
            else:
                df2.to_sql(T_DAILY_BARS, conn, if_exists=if_exists, index=False)
            conn.commit()
            logger.info(f"个股日线写入: {len(df)} 行")
        except Exception:
            conn.rollback()
            logger.exception(f"个股日线写入失败")
            raise
        finally:
            conn.close()

    def get_daily_bars(
        self, symbol: str, start: str = "2000-01-01", end: str = "2099-12-31"
    ) -> pd.DataFrame:
        conn = self._connect()
        try:
            sql = (
                f"SELECT * FROM {T_DAILY_BARS} "
                "WHERE symbol = ? AND date >= ? AND date <= ? "
                "ORDER BY date"
            )
            df = pd.read_sql(sql, conn, params=(symbol, start, end))
            if df.empty:
                return pd.DataFrame()
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")
        finally:
            conn.close()

    def get_last_date(self, table: str, filter_col: str, filter_val: str) -> str | None:
        """查询某表的某个维度（股票/指数代码）的最后日期。

        Fix L: filter_col 白名单校验，防 SQL 注入。
        """
        # 白名单：已知合法列名
        _ALLOWED_COLS = {"symbol", "date", "code", "index_code", "name"}
        if filter_col not in _ALLOWED_COLS:
            raise ValueError(f"filter_col '{filter_col}' 不在白名单 {_ALLOWED_COLS} 中")
        conn = self._connect()
        try:
            cur = conn.execute(
                f"SELECT MAX(date) FROM {table} WHERE {filter_col} = ?",
                (filter_val,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()

    def get_multi_daily_bars(
        self, symbols: list[str], start: str = "2000-01-01", end: str = "2099-12-31"
    ) -> dict[str, pd.DataFrame]:
        """批量获取多只股票日线，返回 {symbol: DataFrame}。"""
        placeholders = ",".join("?" for _ in symbols)
        conn = self._connect()
        try:
            sql = (
                f"SELECT * FROM {T_DAILY_BARS} "
                f"WHERE symbol IN ({placeholders}) AND date >= ? AND date <= ? "
                "ORDER BY symbol, date"
            )
            params = list(symbols) + [start, end]
            df = pd.read_sql(sql, conn, params=params)
            result = {}
            for sym, grp in df.groupby("symbol"):
                grp = grp.copy()
                grp["date"] = pd.to_datetime(grp["date"])
                result[sym] = grp.set_index("date")
            return result
        finally:
            conn.close()

    # ── 指数日线 ──

    def store_index_daily(self, df: pd.DataFrame, if_exists: str = "append"):
        """存入指数日线。df 列: name, date, open, high, low, close, volume"""
        if df.empty:
            return
        conn = self._connect()
        try:
            df.to_sql(T_INDEX_DAILY, conn, if_exists=if_exists, index=False)
            conn.commit()
            logger.info(f"指数日线写入: {len(df)} 行")
        finally:
            conn.close()

    def get_index_daily(
        self, name: str, start: str = "2000-01-01", end: str = "2099-12-31"
    ) -> pd.DataFrame:
        conn = self._connect()
        try:
            sql = (
                f"SELECT * FROM {T_INDEX_DAILY} "
                "WHERE name = ? AND date >= ? AND date <= ? "
                "ORDER BY date"
            )
            df = pd.read_sql(sql, conn, params=(name, start, end))
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                return df.set_index("date")
            return pd.DataFrame()
        finally:
            conn.close()

    def get_all_index_names(self) -> list[str]:
        conn = self._connect()
        try:
            df = pd.read_sql(
                f"SELECT DISTINCT name FROM {T_INDEX_DAILY} ORDER BY name", conn
            )
            return df["name"].tolist()
        finally:
            conn.close()

    # ── 申万行业指数 ──

    def update_sw_index_list(self, df: pd.DataFrame):
        """全量替换申万行业指数列表。df 列: code, name"""
        conn = self._connect()
        try:
            df.to_sql(T_SW_LIST, conn, if_exists="replace", index=False)
            conn.commit()
            logger.info(f"申万行业指数列表更新: {len(df)} 个")
        finally:
            conn.close()

    def get_sw_index_list(self) -> pd.DataFrame:
        conn = self._connect()
        try:
            return pd.read_sql(
                f"SELECT * FROM {T_SW_LIST} ORDER BY code", conn
            )
        finally:
            conn.close()

    def store_sw_index_daily(self, df: pd.DataFrame, if_exists: str = "append"):
        """存入申万行业指数日线。df 列: code, date, open, high, low, close, volume"""
        if df.empty:
            return
        conn = self._connect()
        try:
            df.to_sql(T_SW_DAILY, conn, if_exists=if_exists, index=False)
            conn.commit()
            logger.info(f"申万行业指数写入: {len(df)} 行")
        finally:
            conn.close()

    def get_sw_index_daily(
        self, code: str, start: str = "2000-01-01", end: str = "2099-12-31"
    ) -> pd.DataFrame:
        conn = self._connect()
        try:
            sql = (
                f"SELECT * FROM {T_SW_DAILY} "
                "WHERE code = ? AND date >= ? AND date <= ? "
                "ORDER BY date"
            )
            df = pd.read_sql(sql, conn, params=(code, start, end))
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                return df.set_index("date")
            return pd.DataFrame()
        finally:
            conn.close()

    # ── 更新日志 ──

    def mark_updated(self, table_name: str, row_count: int = 0):
        """记录更新日志。"""
        now = datetime.now().isoformat()
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {T_UPDATE_LOG} (table_name, last_update, row_count) "
                "VALUES (?, ?, ?)",
                (table_name, now, row_count),
            )
            conn.commit()
        finally:
            conn.close()

    def get_last_update(self, table_name: str) -> str | None:
        """获取上次更新时间。"""
        conn = self._connect()
        try:
            cur = conn.execute(
                f"SELECT last_update FROM {T_UPDATE_LOG} WHERE table_name = ?",
                (table_name,),
            )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def needs_update(self, table_name: str, max_age_days: int = 5) -> bool:
        """检查是否需要更新（超过 max_age_days 未更新）。"""
        last = self.get_last_update(table_name)
        if last is None:
            return True
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).days >= max_age_days

    # ── 交易日历 ──

    def update_trade_calendar(self, df: pd.DataFrame):
        """df 列: date, is_trading"""
        conn = self._connect()
        try:
            df.to_sql(T_TRADE_CAL, conn, if_exists="replace", index=False)
            conn.commit()
            logger.info(f"交易日历更新: {len(df)} 条")
        finally:
            conn.close()

    def is_trading_day(self, d: str | date) -> bool:
        conn = self._connect()
        try:
            d_str = d.isoformat() if isinstance(d, date) else d
            cur = conn.execute(
                f"SELECT is_trading FROM {T_TRADE_CAL} WHERE date = ?", (d_str,)
            )
            row = cur.fetchone()
            return row is not None and row[0] == 1
        finally:
            conn.close()

    # ── 统计 ──

    def table_stats(self) -> dict:
        """返回各表行数统计。"""
        conn = self._connect()
        try:
            stats = {}
            for tbl in [T_STOCK_LIST, T_DAILY_BARS, T_INDEX_DAILY, T_SW_LIST, T_SW_DAILY, T_VALUATION]:
                try:
                    cur = conn.execute(f"SELECT COUNT(*) FROM {tbl}")
                    stats[tbl] = cur.fetchone()[0]
                except Exception:
                    stats[tbl] = 0
            return stats
        finally:
            conn.close()

    # ── 估值数据 ──

    def get_valuation(self, date: str | None = None) -> pd.DataFrame:
        """读取估值数据。

        Args:
            date: 指定日期 YYYY-MM-DD。为 None 时取最新快照。

        Returns:
            DataFrame, 列为 symbol, date, name, pe_ttm, pb, total_mv, float_mv, turnover。
            无数据时返回空 DataFrame。
        """
        conn = self._connect()
        try:
            if date is None:
                sql = f"SELECT * FROM {T_VALUATION} WHERE date = (SELECT MAX(date) FROM {T_VALUATION})"
            else:
                sql = f"SELECT * FROM {T_VALUATION} WHERE date = ? ORDER BY symbol"
            df = pd.read_sql(sql, conn) if date is None else pd.read_sql(sql, conn, params=(date,))
            return df
        finally:
            conn.close()

    def update_valuation(self, df: pd.DataFrame) -> int:
        """追加估值快照。已有 (symbol, date) 组合时跳过（INSERT OR IGNORE）。

        Args:
            df: DataFrame, 必须含 symbol, date, name, pe_ttm, pb, total_mv, float_mv, turnover

        Returns:
            写入行数
        """
        if df.empty:
            return 0
        conn = self._connect()
        try:
            cols = ", ".join(f'"{c}"' for c in df.columns)
            ph = ", ".join("?" for _ in df.columns)
            data = [tuple(row) for row in df[df.columns].to_numpy()]
            conn.executemany(
                f"INSERT OR IGNORE INTO {T_VALUATION} ({cols}) VALUES ({ph})",
                data,
            )
            conn.commit()
            logger.info(f"估值数据写入: {len(df)} 行")
            return len(df)
        except Exception:
            logger.exception("估值数据写入失败")
            raise
        finally:
            conn.close()

    # ── intraday_snapshot 盘中快照 ──

    def store_intraday_snapshot(self, df: pd.DataFrame):
        """写入盘中快照。

        df 列: symbol, date, time, price, open, high, low, pre_close,
               volume, amount, bid1~5, ask1~5, bid_vol1~5, ask_vol1~5, source
        """
        if df.empty:
            return
        conn = self._connect()
        try:
            cols = ", ".join(f'"{c}"' for c in df.columns)
            ph = ", ".join("?" for _ in df.columns)
            data = [tuple(row) for row in df[df.columns].to_numpy()]
            conn.executemany(
                f"INSERT OR REPLACE INTO {T_INTRA_SNAPSHOT} ({cols}) VALUES ({ph})",
                data,
            )
            conn.commit()
            logger.info(f"盘中快照写入: {len(df)} 行")
        finally:
            conn.close()

    def get_intraday_snapshot(self, symbol: str, date: str) -> pd.DataFrame:
        """获取指定股票当日盘中快照"""
        conn = self._connect()
        try:
            df = pd.read_sql(
                f"SELECT * FROM {T_INTRA_SNAPSHOT} WHERE symbol=? AND date=? ORDER BY time",
                conn, params=(symbol, date),
            )
            return df if not df.empty else pd.DataFrame()
        finally:
            conn.close()

    def get_intraday_snapshot_by_date(self, date: str) -> pd.DataFrame:
        """获取指定日期全部股票的盘中快照（用于持仓估值）"""
        conn = self._connect()
        try:
            df = pd.read_sql(
                f"SELECT symbol, price, volume, amount, time FROM {T_INTRA_SNAPSHOT} "
                "WHERE date=? ORDER BY symbol",
                conn, params=(date,),
            )
            return df if not df.empty else pd.DataFrame()
        finally:
            conn.close()
