"""信号数据存储 — 与 LocalDataWarehouse 共享同一 SQLite 文件"""

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from sniper.signals.schema import (
    DB_FILE, ALL_SIGNAL_DDLS,
    T_NORTHBOUND, T_FUND_FLOW, T_DRAGON_TIGER,
    T_INDUSTRY_COMPARE, T_HOT_STOCKS, T_QUARTERLY,
)
from core.logger import get_logger

logger = get_logger("sniper.signals.store")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class SignalStore:
    """信号数据存储 — 与 LocalDataWarehouse 共享同一 SQLite 数据库。

    管理 6 张信号表：北向资金、资金流向、龙虎榜、行业对比、强势股、季报。
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or PROJECT_ROOT / DB_FILE)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self):
        """初始化信号表结构（幂等）。"""
        conn = self._connect()
        try:
            for ddl in ALL_SIGNAL_DDLS:
                conn.execute(ddl)
            conn.commit()
        finally:
            conn.close()
        logger.info(f"信号表就绪: {self.db_path}")

    # ── 通用写入 ──

    def _store(self, table: str, df: pd.DataFrame, if_exists: str = "append"):
        if df.empty:
            return
        conn = self._connect()
        try:
            # 清除已有数据中与本次写入相同日期的行（对带 date 列的表）
            date_cols = {
                T_NORTHBOUND: "date", T_FUND_FLOW: "date",
                T_DRAGON_TIGER: "date", T_INDUSTRY_COMPARE: "date",
                T_HOT_STOCKS: "date",
            }
            if if_exists == "append" and table in date_cols and "date" in df.columns:
                dates = df["date"].unique()
                placeholders = ",".join("?" for _ in dates)
                conn.execute(f"DELETE FROM {table} WHERE date IN ({placeholders})", dates)
                conn.commit()
            df.to_sql(table, conn, if_exists=if_exists, index=False)
            conn.commit()
            logger.info(f"[信号] {table}: 写入 {len(df)} 行")
        finally:
            conn.close()

    def _count(self, table: str) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
        finally:
            conn.close()

    def _query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        conn = self._connect()
        try:
            return pd.read_sql(sql, conn, params=params)
        finally:
            conn.close()

    # ── 北向资金 ──

    def store_northbound(self, df: pd.DataFrame):
        """df 列: date, time, sh_net, sz_net, total"""
        self._store(T_NORTHBOUND, df)

    def get_northbound(self, start: str = "", end: str = "") -> pd.DataFrame:
        sql = f"SELECT * FROM {T_NORTHBOUND}"
        params: tuple = ()
        if start and end:
            sql += " WHERE date >= ? AND date <= ? ORDER BY date, time"
            params = (start, end)
        elif start:
            sql += " WHERE date >= ? ORDER BY date, time"
            params = (start,)
        else:
            sql += " ORDER BY date, time"
        return self._query(sql, params)

    def get_northbound_daily(self, start: str = "", end: str = "") -> pd.DataFrame:
        """按日聚合北向资金净流入。"""
        sql = f"""SELECT date, SUM(total) as total_net
                  FROM {T_NORTHBOUND}
                  WHERE date >= ? AND date <= ?
                  GROUP BY date ORDER BY date"""
        return self._query(sql, (start, end))

    # ── 资金流向 ──

    def store_fund_flow(self, df: pd.DataFrame):
        """df 列: symbol, date, main_net, retail_net, super_large, large_net, medium_net, small_net"""
        self._store(T_FUND_FLOW, df)

    def get_fund_flow(self, symbol: str, start: str = "", end: str = "") -> pd.DataFrame:
        sql = f"SELECT * FROM {T_FUND_FLOW} WHERE symbol = ?"
        params: list[Any] = [symbol]
        if start and end:
            sql += " AND date >= ? AND date <= ?"
            params += [start, end]
        sql += " ORDER BY date"
        return self._query(sql, tuple(params))

    def get_fund_flow_batch(self, symbols: list[str], date: str) -> pd.DataFrame:
        """批量获取某日多只股票的资金流向。"""
        placeholders = ",".join("?" for _ in symbols)
        sql = f"""SELECT * FROM {T_FUND_FLOW}
                  WHERE symbol IN ({placeholders}) AND date = ?"""
        return self._query(sql, tuple(symbols) + (date,))

    # ── 龙虎榜 ──

    def store_dragon_tiger(self, df: pd.DataFrame):
        """df 列: date, symbol, reason, net_buy, buy_amount, sell_amount,
                  institution_buy, institution_sell, turnover_rate"""
        self._store(T_DRAGON_TIGER, df)

    def get_dragon_tiger(self, date: str) -> pd.DataFrame:
        return self._query(
            f"SELECT * FROM {T_DRAGON_TIGER} WHERE date = ? ORDER BY net_buy DESC",
            (date,),
        )

    def get_dragon_tiger_range(self, start: str, end: str) -> pd.DataFrame:
        return self._query(
            f"SELECT * FROM {T_DRAGON_TIGER} WHERE date >= ? AND date <= ? ORDER BY date, net_buy DESC",
            (start, end),
        )

    # ── 行业对比 ──

    def store_industry_compare(self, df: pd.DataFrame):
        """df 列: date, industry_name, daily_change, volume_change,
                  leader_symbol, leader_change, rank, stock_count"""
        self._store(T_INDUSTRY_COMPARE, df)

    def get_industry_compare(self, date: str) -> pd.DataFrame:
        return self._query(
            f"SELECT * FROM {T_INDUSTRY_COMPARE} WHERE date = ? ORDER BY rank",
            (date,),
        )

    def get_industry_compare_range(self, start: str, end: str) -> pd.DataFrame:
        return self._query(
            f"SELECT * FROM {T_INDUSTRY_COMPARE} WHERE date >= ? AND date <= ? ORDER BY date, rank",
            (start, end),
        )

    # ── 强势股 ──

    def store_hot_stocks(self, df: pd.DataFrame):
        """df 列: date, symbol, reason_tags"""
        self._store(T_HOT_STOCKS, df)

    def get_hot_stocks(self, date: str) -> pd.DataFrame:
        return self._query(
            f"SELECT * FROM {T_HOT_STOCKS} WHERE date = ?", (date,)
        )

    # ── 季报 ──

    def store_quarterly(self, df: pd.DataFrame):
        """df 列: symbol, report_date, eps, roe, net_profit, net_profit_yoy,
                  revenue, revenue_yoy, gross_margin, pe_ttm, pb, debt_to_assets,
                  free_cash_flow"""
        self._store(T_QUARTERLY, df)

    def get_quarterly(self, symbol: str, report_date: str = "") -> pd.DataFrame:
        sql = f"SELECT * FROM {T_QUARTERLY} WHERE symbol = ?"
        params: list[Any] = [symbol]
        if report_date:
            sql += " AND report_date = ?"
            params.append(report_date)
        sql += " ORDER BY report_date DESC"
        return self._query(sql, tuple(params))

    def get_latest_quarterly_batch(self, symbols: list[str], as_of: str) -> pd.DataFrame:
        """批量获取多只股票在 as_of 日期前的最新季报。"""
        placeholders = ",".join("?" for _ in symbols)
        sql = f"""SELECT a.* FROM {T_QUARTERLY} a
                  INNER JOIN (
                      SELECT symbol, MAX(report_date) as max_date
                      FROM {T_QUARTERLY}
                      WHERE symbol IN ({placeholders}) AND report_date <= ?
                      GROUP BY symbol
                  ) b ON a.symbol = b.symbol AND a.report_date = b.max_date"""
        return self._query(sql, tuple(symbols) + (as_of,))

    # ── 统计 ──

    def table_stats(self) -> dict[str, int]:
        stats = {}
        for tbl in [T_NORTHBOUND, T_FUND_FLOW, T_DRAGON_TIGER,
                     T_INDUSTRY_COMPARE, T_HOT_STOCKS, T_QUARTERLY]:
            try:
                stats[tbl] = self._count(tbl)
            except Exception:
                stats[tbl] = 0
        return stats
