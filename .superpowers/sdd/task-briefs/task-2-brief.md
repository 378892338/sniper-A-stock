# Task 2: PipelineJournal 日志系统

**Files:**
- Create: `data/local/pipeline_journal.py`
- Create: `tests/test_pipeline_journal.py`

**Interfaces:**
- Consumes: `LocalDataWarehouse._connect()` (复用同一 SQLite 文件)
- Produces: `class PipelineJournal` with methods: `start_run`, `log`, `get_last_fetch`, `has_write_since`, `get_missing_stocks`, `get_missing_stocks_fast`, `get_run_summary`, `get_run_progress`, `cleanup`

## Implementation

Create `data/local/pipeline_journal.py` with the PipelineJournal class:

```python
"""管线审计日志 — 每步操作的时间戳记录 + 恢复查询 + 清理归档

所有读写通过此接口，禁止直接操作 pipeline_journal 表。
与 LocalDataWarehouse 共用同一 SQLite 文件。
"""

import json
import uuid
from datetime import datetime

import pandas as pd

from core.logger import get_logger

logger = get_logger("data.pipeline_journal")

_STEP_ORDER = ["probe", "validate", "fetch", "write", "verify"]


class PipelineJournal:
    """管线审计日志系统"""

    def __init__(self, db_path: str | None = None):
        from data.local.warehouse import LocalDataWarehouse
        self._wh = LocalDataWarehouse(db_path)

    def _conn(self):
        return self._wh._connect()

    def start_run(self, mode: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        logger.info(f"Pipeline run started: {run_id} mode={mode}")
        return run_id

    def log(self, run_id: str, step: str, symbol: str, status: str,
            detail: dict | None = None, source: str | None = None,
            data_start: str | None = None, data_end: str | None = None,
            rows_count: int | None = None, error_msg: str | None = None,
            elapsed_ms: int | None = None):
        if step not in _STEP_ORDER:
            logger.warning(f"未知 step: {step}")
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO pipeline_journal
                   (run_id, mode, step, symbol, timestamp, status,
                    elapsed_ms, detail, source, data_start, data_end,
                    rows_count, error_msg)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, self._infer_mode(run_id), step, symbol,
                 datetime.now().isoformat(), status,
                 elapsed_ms, json.dumps(detail or {}, ensure_ascii=False),
                 source, data_start, data_end, rows_count, error_msg),
            )
            conn.commit()
        finally:
            conn.close()

    def _infer_mode(self, run_id: str) -> str:
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT mode FROM pipeline_journal WHERE run_id=? LIMIT 1", (run_id,))
            row = cur.fetchone()
            return row[0] if row else "unknown"
        finally:
            conn.close()

    def get_last_fetch(self, symbol: str) -> dict | None:
        conn = self._conn()
        try:
            cur = conn.execute(
                """SELECT step, timestamp, status, data_end, data_start,
                          rows_count, source, elapsed_ms, error_msg
                   FROM pipeline_journal
                   WHERE symbol=? AND step='write' AND status='ok'
                   ORDER BY data_end DESC LIMIT 1""", (symbol,))
            row = cur.fetchone()
            if not row:
                return None
            keys = ["step", "timestamp", "status", "data_end",
                    "data_start", "rows_count", "source", "elapsed_ms", "error_msg"]
            return dict(zip(keys, row))
        finally:
            conn.close()

    def has_write_since(self, symbol: str, since_date: str) -> bool:
        conn = self._conn()
        try:
            cur = conn.execute(
                """SELECT 1 FROM pipeline_journal
                   WHERE symbol=? AND step='write' AND status='ok'
                     AND data_end >= ? LIMIT 1""", (symbol, since_date))
            return cur.fetchone() is not None
        finally:
            conn.close()

    def get_missing_stocks(self, symbols: list[str], today: str) -> list[str]:
        need_fetch = []
        for sym in symbols:
            if not self.has_write_since(sym, today):
                need_fetch.append(sym)
        return need_fetch

    def get_missing_stocks_fast(self, symbols: list[str], today: str) -> list[str]:
        if not symbols:
            return []
        conn = self._conn()
        try:
            placeholders = ",".join("?" for _ in symbols)
            df = pd.read_sql(
                f"""SELECT symbol, MAX(data_end) as last_end
                    FROM pipeline_journal
                    WHERE symbol IN ({placeholders})
                      AND step='write' AND status='ok'
                    GROUP BY symbol""",
                conn, params=symbols,
            )
        finally:
            conn.close()
        up_to_date = set(df[df["last_end"] >= today]["symbol"].tolist()) if not df.empty else set()
        return [s for s in symbols if s not in up_to_date]

    def get_run_summary(self, run_id: str) -> dict:
        conn = self._conn()
        try:
            df = pd.read_sql(
                "SELECT step, status, COUNT(*) as cnt FROM pipeline_journal "
                "WHERE run_id=? GROUP BY step, status", conn, params=(run_id,))
        finally:
            conn.close()
        by_step = {}
        total_ok = total_fail = total_skip = 0
        if not df.empty:
            for _, row in df.iterrows():
                by_step.setdefault(row["step"], {})[row["status"]] = int(row["cnt"])
                if row["status"] == "ok":
                    total_ok += int(row["cnt"])
                elif row["status"] == "fail":
                    total_fail += int(row["cnt"])
                elif row["status"] == "skip":
                    total_skip += int(row["cnt"])
        return {"total": total_ok + total_fail + total_skip, "ok": total_ok,
                "fail": total_fail, "skip": total_skip, "by_step": by_step}

    def get_run_progress(self, run_id: str) -> dict:
        conn = self._conn()
        try:
            total = pd.read_sql(
                "SELECT COUNT(DISTINCT symbol) as n FROM pipeline_journal WHERE run_id=?",
                conn, params=(run_id,)).iloc[0]["n"]
            done = pd.read_sql(
                "SELECT COUNT(DISTINCT symbol) as n FROM pipeline_journal "
                "WHERE run_id=? AND step='write'", conn, params=(run_id,)).iloc[0]["n"]
            ok_count = pd.read_sql(
                "SELECT COUNT(*) as n FROM pipeline_journal "
                "WHERE run_id=? AND step='write' AND status='ok'",
                conn, params=(run_id,)).iloc[0]["n"]
            fail_count = pd.read_sql(
                "SELECT COUNT(*) as n FROM pipeline_journal "
                "WHERE run_id=? AND step='write' AND status='fail'",
                conn, params=(run_id,)).iloc[0]["n"]
        finally:
            conn.close()
        return {"total": int(total), "done": int(done), "ok": int(ok_count),
                "fail": int(fail_count),
                "progress_pct": round(done / total * 100, 1) if total > 0 else 0}

    def cleanup(self, keep_days: int = 30):
        conn = self._conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO pipeline_daily_summary
                    (date, symbol, fetch_ok, fetch_fail, verify_ok, verify_fail,
                     source, data_start, data_end)
                    SELECT substr(timestamp,1,10), symbol,
                           MAX(CASE WHEN step='fetch' AND status='ok' THEN 1 ELSE 0 END),
                           MAX(CASE WHEN step='fetch' AND status='fail' THEN 1 ELSE 0 END),
                           MAX(CASE WHEN step='verify' AND status='ok' THEN 1 ELSE 0 END),
                           MAX(CASE WHEN step='verify' AND status='fail' THEN 1 ELSE 0 END),
                           MAX(source), MIN(data_start), MAX(data_end)
                    FROM pipeline_journal
                    WHERE timestamp < date('now', ?)
                    GROUP BY substr(timestamp,1,10), symbol""",
                (f"-{keep_days} days",))
            conn.execute(
                "DELETE FROM pipeline_journal WHERE timestamp < date('now', ?)",
                (f"-{keep_days} days",))
            conn.commit()
            logger.info(f"Journal cleanup: keep_days={keep_days}")
        finally:
            conn.close()
```

## Tests

Create `tests/test_pipeline_journal.py`:

```python
from data.local.pipeline_journal import PipelineJournal


def test_journal_lifecycle():
    journal = PipelineJournal()
    run_id = journal.start_run("test")
    journal.log(run_id, "probe", "000001", "ok")
    journal.log(run_id, "fetch", "000001", "ok",
                source="mootdx", data_start="2026-01-01", data_end="2026-06-25",
                rows_count=176, elapsed_ms=350)
    journal.log(run_id, "write", "000001", "ok",
                data_start="2026-01-01", data_end="2026-06-25",
                rows_count=176, elapsed_ms=120)

    last = journal.get_last_fetch("000001")
    assert last is not None
    assert last["data_end"] == "2026-06-25"
    assert last["source"] == "mootdx"

    summary = journal.get_run_summary(run_id)
    assert summary["total"] == 3
    assert summary["ok"] == 3

    progress = journal.get_run_progress(run_id)
    assert progress["done"] == 1


def test_has_write_since():
    journal = PipelineJournal()
    run_id = journal.start_run("test2")
    journal.log(run_id, "write", "000001", "ok",
                data_start="2026-01-01", data_end="2026-06-25")
    journal.log(run_id, "write", "600519", "ok",
                data_start="2026-01-01", data_end="2026-06-26")

    assert journal.has_write_since("000001", "2026-06-26") is False
    assert journal.has_write_since("600519", "2026-06-26") is True
    assert journal.has_write_since("999999", "2026-06-26") is False


def test_get_missing_stocks_fast():
    journal = PipelineJournal()
    run_id = journal.start_run("test3")
    journal.log(run_id, "write", "000001", "ok",
                data_start="2026-01-01", data_end="2026-06-25")
    journal.log(run_id, "write", "600519", "ok",
                data_start="2026-01-01", data_end="2026-06-26")
    journal.log(run_id, "write", "600036", "fail",
                data_start="2026-01-01", data_end="2026-06-26")

    symbols = ["000001", "600519", "600036", "601318"]
    missing = journal.get_missing_stocks_fast(symbols, "2026-06-26")
    assert "600519" not in missing
    assert "000001" in missing
    assert "600036" in missing
    assert "601318" in missing


def test_cleanup():
    journal = PipelineJournal()
    conn = journal._conn()
    conn.execute(
        "INSERT INTO pipeline_journal(run_id,mode,step,symbol,timestamp,status)"
        "VALUES('old','test','write','000001','2020-01-01T00:00:00','ok')")
    conn.commit()
    conn.close()
    journal.cleanup(keep_days=1)
    last = journal.get_last_fetch("000001")
    assert last is None
```

## Verification

```bash
cd /d/projects/quant-system && python -m pytest tests/test_pipeline_journal.py -v
```

Expected: 4 passed

## Commit

```bash
git add data/local/pipeline_journal.py tests/test_pipeline_journal.py
git commit -m "feat(journal): add PipelineJournal with recovery, progress, cleanup"
```
