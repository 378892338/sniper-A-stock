# Task 1: Schema 扩展 — 新增 intraday_snapshot + pipeline_journal 表

**Files:**
- Modify: `data/local/schema.py` — 新增 DDL + 索引
- Verify: 不破坏现有表结构

## What to do

### 1. In schema.py, add the three new DDL strings

After the `CREATE_VALUATION` line, add:

```python
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
CREATE INDEX IF NOT EXISTS idx_journal_lookup
    ON pipeline_journal(symbol, step, status, data_end);
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
```

### 2. Update ALL_DDLS list

Add the three new DDLs to the ALL_DDLS list (at the end, after CREATE_VALUATION).

### 3. Add table name constants

Add these lines at the bottom of the file:

```python
T_INTRA_SNAPSHOT = "intraday_snapshot"
T_PIPELINE_JOURNAL = "pipeline_journal"
T_DAILY_SUMMARY = "pipeline_daily_summary"
```

### 4. Verification

Run this probe to confirm the tables were created:

```bash
cd /d/projects/quant-system && python -c "
from data.local.warehouse import LocalDataWarehouse
wh = LocalDataWarehouse()
conn = wh._connect()
cur = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\")
tables = [r[0] for r in cur.fetchall()]
print('Tables:', tables)
assert 'intraday_snapshot' in tables, 'intraday_snapshot 表未创建'
assert 'pipeline_journal' in tables, 'pipeline_journal 表未创建'
assert 'pipeline_daily_summary' in tables, 'pipeline_daily_summary 表未创建'
conn.close()
print('Schema 验证通过')
"
```

Expected: `Tables: [..., intraday_snapshot, pipeline_journal, pipeline_daily_summary, ...]` + `Schema 验证通过`

### 5. Commit

```bash
git add data/local/schema.py
git commit -m "feat(schema): add intraday_snapshot, pipeline_journal, daily_summary tables with indexes"
```
