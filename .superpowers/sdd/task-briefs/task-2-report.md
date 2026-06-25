# Task 2 Report: PipelineJournal 日志系统

**Status:** DONE

## What was implemented

- `data/local/pipeline_journal.py` — `PipelineJournal` class with full journal API:
  - `start_run()` — generate run_id, log start
  - `log()` — insert journal entry with step, symbol, status, detail, source, data range, rows, error, elapsed
  - `get_last_fetch()` — query last successful fetch for a symbol
  - `has_write_since()` — check if symbol has write since given date
  - `get_missing_stocks()` / `get_missing_stocks_fast()` — find symbols needing fetch
  - `get_run_summary()` — aggregate counts by step/status
  - `get_run_progress()` — distinct symbol progress with percentage
  - `cleanup()` — archive to pipeline_daily_summary + delete old records
- `tests/test_pipeline_journal.py` — 4 tests covering lifecycle, write-since, missing-stocks-fast, cleanup

## Test results

```
tests/test_pipeline_journal.py::test_journal_lifecycle PASSED
tests/test_pipeline_journal.py::test_has_write_since PASSED
tests/test_pipeline_journal.py::test_get_missing_stocks_fast PASSED
tests/test_pipeline_journal.py::test_cleanup PASSED

4 passed in 0.57s
```

## Files changed

- `data/local/pipeline_journal.py` (created)
- `tests/test_pipeline_journal.py` (created)

## Deviations from brief

1. **`get_last_fetch` SQL: `step='write'` changed to `step='fetch'`**
   - Reason: The method is named `get_last_fetch` and the test asserts `last["source"] == "mootdx"`. The `source` field is only populated on the `fetch` step, not the `write` step. Querying `step='write'` returned `source=None`, causing test failure. Changing to `step='fetch'` matches both the method name and the test expectation.

2. **`test_cleanup`: symbol changed from `'000001'` to `'cleanup_sym'`, step changed from `'write'` to `'fetch'`**
   - Reason: All tests share the same SQLite database (`meta.db`). Other tests insert `'000001'` records with today's timestamp that survive cleanup. Using a unique symbol isolates the cleanup test. Additionally, since `get_last_fetch` now queries `step='fetch'`, the cleanup test's direct insert must use `step='fetch'` for the query to find and (after cleanup) not find the record.

## Self-review

- Code follows the brief's structure with only the two bugfixes above
- All methods correctly delegate to `LocalDataWarehouse._connect()` for DB access
- Connection lifecycle managed with try/finally close pattern
- `_infer_mode` gracefully returns "unknown" for first log entry in a run
