"""本地数据仓库 schema 定义

DAILY_BARS_COLUMNS 可与 sniper/config.py 的 FACTOR_REQUIRED_FIELDS 同步，
保证因子需要什么，数据库就存什么。
"""

from core.logger import get_logger

logger = get_logger("data.local.schema")

# 数据库文件路径
DB_FILE = "data/local/meta.db"

# ── DDL ──

CREATE_STOCK_LIST = """
CREATE TABLE IF NOT EXISTS stock_list (
    symbol  TEXT PRIMARY KEY,
    name    TEXT,
    list_date TEXT,
    status  TEXT DEFAULT 'active'
);
"""

CREATE_TRADE_CALENDAR = """
CREATE TABLE IF NOT EXISTS trade_calendar (
    date         TEXT PRIMARY KEY,
    is_trading   INTEGER NOT NULL DEFAULT 1
);
"""

CREATE_DAILY_BARS = """
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume REAL,
    amount REAL,
    turnover REAL,
    PRIMARY KEY (symbol, date)
);
"""

CREATE_INDEX_DAILY = """
CREATE TABLE IF NOT EXISTS index_daily (
    name   TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume REAL,
    PRIMARY KEY (name, date)
);
"""

CREATE_SW_INDEX_LIST = """
CREATE TABLE IF NOT EXISTS sw_index_list (
    code TEXT PRIMARY KEY,
    name TEXT
);
"""

CREATE_SW_INDEX_DAILY = """
CREATE TABLE IF NOT EXISTS sw_index_daily (
    code   TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume REAL,
    PRIMARY KEY (code, date)
);
"""

CREATE_UPDATE_LOG = """
CREATE TABLE IF NOT EXISTS update_log (
    table_name  TEXT PRIMARY KEY,
    last_update TEXT,
    row_count   INTEGER DEFAULT 0
);
"""

CREATE_VALUATION = """
CREATE TABLE IF NOT EXISTS stock_valuation (
    symbol   TEXT,
    date     TEXT,
    name     TEXT,
    pe_ttm   REAL,
    pb       REAL,
    total_mv REAL,
    float_mv REAL,
    turnover REAL,
    PRIMARY KEY (symbol, date)
);
"""

CREATE_INTRA_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS intraday_snapshot (
    symbol    TEXT NOT NULL,
    date      TEXT NOT NULL,
    time      TEXT NOT NULL,
    price     REAL,
    open      REAL,
    high      REAL,
    low       REAL,
    pre_close REAL,
    volume    REAL,
    amount    REAL,
    bid1      REAL,  ask1     REAL,
    bid_vol1  REAL,  ask_vol1 REAL,
    bid2      REAL,  ask2     REAL,
    bid_vol2  REAL,  ask_vol2 REAL,
    bid3      REAL,  ask3     REAL,
    bid_vol3  REAL,  ask_vol3 REAL,
    bid4      REAL,  ask4     REAL,
    bid_vol4  REAL,  ask_vol4 REAL,
    bid5      REAL,  ask5     REAL,
    bid_vol5  REAL,  ask_vol5 REAL,
    source    TEXT DEFAULT 'mootdx',
    PRIMARY KEY (symbol, date, time)
);
"""

CREATE_PIPELINE_JOURNAL = """
CREATE TABLE IF NOT EXISTS pipeline_journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    mode       TEXT NOT NULL,
    step       TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    timestamp  TEXT NOT NULL,
    status     TEXT NOT NULL,
    elapsed_ms INTEGER,
    detail     TEXT,
    source     TEXT,
    data_start TEXT,
    data_end   TEXT,
    rows_count INTEGER,
    error_msg  TEXT
);
"""

CREATE_IDX_JOURNAL_LOOKUP = """
CREATE INDEX IF NOT EXISTS idx_journal_lookup
    ON pipeline_journal(symbol, step, status, data_end);
"""

CREATE_IDX_JOURNAL_RUN = """
CREATE INDEX IF NOT EXISTS idx_journal_run
    ON pipeline_journal(run_id, step);
"""

CREATE_DAILY_SUMMARY = """
CREATE TABLE IF NOT EXISTS pipeline_daily_summary (
    date      TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    fetch_ok  INTEGER DEFAULT 0,
    fetch_fail INTEGER DEFAULT 0,
    verify_ok INTEGER DEFAULT 0,
    verify_fail INTEGER DEFAULT 0,
    source    TEXT,
    data_start TEXT,
    data_end   TEXT,
    PRIMARY KEY (date, symbol)
);
"""

ALL_DDLS = [
    CREATE_STOCK_LIST,
    CREATE_TRADE_CALENDAR,
    CREATE_DAILY_BARS,
    CREATE_INDEX_DAILY,
    CREATE_SW_INDEX_LIST,
    CREATE_SW_INDEX_DAILY,
    CREATE_UPDATE_LOG,
    CREATE_VALUATION,
    CREATE_INTRA_SNAPSHOT,
    CREATE_PIPELINE_JOURNAL,
    CREATE_IDX_JOURNAL_LOOKUP,
    CREATE_IDX_JOURNAL_RUN,
    CREATE_DAILY_SUMMARY,
]

# ── 表名常量 ──
T_STOCK_LIST = "stock_list"
T_TRADE_CAL = "trade_calendar"
T_DAILY_BARS = "daily_bars"
T_INDEX_DAILY = "index_daily"
T_SW_LIST = "sw_index_list"
T_SW_DAILY = "sw_index_daily"
T_UPDATE_LOG = "update_log"
T_VALUATION = "stock_valuation"
T_INTRA_SNAPSHOT = "intraday_snapshot"
T_PIPELINE_JOURNAL = "pipeline_journal"
T_DAILY_SUMMARY = "pipeline_daily_summary"

# ── 列定义（用于动态 schema 对齐）──
# 随 sniper/config.py 的 FACTOR_REQUIRED_FIELDS 扩展
DAILY_BARS_COLUMNS = {
    "symbol": "TEXT",
    "date": "TEXT",
    "open": "REAL",
    "high": "REAL",
    "low": "REAL",
    "close": "REAL",
    "volume": "REAL",
    "amount": "REAL",
    "turnover": "REAL",
}


def ensure_schema(warehouse, required_fields: list[str] | None = None,
                  desired_fields: list[str] | None = None):
    """确保 daily_bars 表包含所有 required_fields 和 desired_fields。

    如果因子加了新字段（如 turnover），
    自动执行 ALTER TABLE，不影响现有数据。
    新增列初始全 NULL，后续下载填充。

    Args:
        warehouse: LocalDataWarehouse 实例
        required_fields: 若为 None 则从 sniper/config.py 读取
        desired_fields: 若为 None 则从 sniper/config.py 读取

    Returns:
        {"missing": list, "added": list, "ok": bool}
    """
    import sqlite3

    if required_fields is None or desired_fields is None:
        try:
            from sniper.config import FACTOR_REQUIRED_FIELDS, FACTOR_DESIRED_FIELDS
            if required_fields is None:
                required_fields = FACTOR_REQUIRED_FIELDS
            if desired_fields is None:
                desired_fields = FACTOR_DESIRED_FIELDS
        except ImportError:
            required_fields = required_fields or []
            desired_fields = desired_fields or []

    all_fields = list(required_fields) + list(desired_fields)

    conn = warehouse._connect()
    try:
        cur = conn.execute(f"PRAGMA table_info({T_DAILY_BARS})")
        existing = {row[1] for row in cur.fetchall()}
        missing = [f for f in all_fields if f not in existing]

        for col in missing:
            col_type = DAILY_BARS_COLUMNS.get(col, "REAL")
            try:
                conn.execute(f"ALTER TABLE {T_DAILY_BARS} ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass  # 列已存在（并发安全）
        conn.commit()

        if missing:
            logger.info(f"schema 自动扩展: {missing}")

        return {"missing": missing, "added": missing, "ok": True}
    finally:
        conn.close()
