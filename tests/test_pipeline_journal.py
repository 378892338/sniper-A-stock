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
        "VALUES('old','test','fetch','cleanup_sym','2020-01-01T00:00:00','ok')")
    conn.commit()
    conn.close()
    journal.cleanup(keep_days=1)
    last = journal.get_last_fetch("cleanup_sym")
    assert last is None
