"""PipelineJournal 单元测试 — 使用唯一 symbol 防跨测试污染"""

from data.local.pipeline_journal import PipelineJournal


def test_journal_lifecycle():
    journal = PipelineJournal()
    run_id = journal.start_run("test")
    sym = "LIFECYCLE_001"
    journal.log(run_id, "probe", sym, "ok")
    journal.log(run_id, "fetch", sym, "ok",
                source="mootdx", data_start="2026-01-01", data_end="2026-06-25",
                rows_count=176, elapsed_ms=350)
    journal.log(run_id, "write", sym, "ok",
                data_start="2026-01-01", data_end="2026-06-25",
                rows_count=176, elapsed_ms=120)

    last = journal.get_last_fetch(sym)
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
    ws_a = "WRITESINCE_A"
    ws_b = "WRITESINCE_B"
    ws_c = "WRITESINCE_C"
    journal.log(run_id, "write", ws_a, "ok",
                data_start="2026-01-01", data_end="2026-06-25")
    journal.log(run_id, "write", ws_b, "ok",
                data_start="2026-01-01", data_end="2026-06-26")

    assert journal.has_write_since(ws_a, "2026-06-26") is False
    assert journal.has_write_since(ws_b, "2026-06-26") is True
    assert journal.has_write_since(ws_c, "2026-06-26") is False


def test_get_missing_stocks_fast():
    journal = PipelineJournal()
    run_id = journal.start_run("test3")
    ms_a = "MISSING_A"
    ms_b = "MISSING_B"
    ms_c = "MISSING_C"
    ms_d = "MISSING_D"
    journal.log(run_id, "write", ms_a, "ok",
                data_start="2026-01-01", data_end="2026-06-25")
    journal.log(run_id, "write", ms_b, "ok",
                data_start="2026-01-01", data_end="2026-06-26")
    journal.log(run_id, "write", ms_c, "fail",
                data_start="2026-01-01", data_end="2026-06-26")

    symbols = [ms_a, ms_b, ms_c, ms_d]
    missing = journal.get_missing_stocks_fast(symbols, "2026-06-26")
    assert ms_b not in missing  # ok + data_end >= today
    assert ms_a in missing      # ok but data_end < today
    assert ms_c in missing      # fail
    assert ms_d in missing      # no record


def test_cleanup():
    journal = PipelineJournal()
    conn = journal._conn()
    conn.execute(
        "INSERT INTO pipeline_journal(run_id,mode,step,symbol,timestamp,status)"
        "VALUES('cleanup_run','test','fetch','CLEANUP_SYM','2020-01-01T00:00:00','ok')")
    conn.commit()
    conn.close()

    journal.cleanup(keep_days=1)

    last = journal.get_last_fetch("CLEANUP_SYM")
    assert last is None
