"""Tests for StreamEngine."""

from data.stream_engine import StreamEngine


def test_chunk():
    engine = StreamEngine(journal=None, warehouse=None)
    items = list(range(13))
    chunks = engine._chunk(items, 5)
    assert len(chunks) == 5
    assert sum(len(c) for c in chunks) == 13


def test_should_verify():
    engine = StreamEngine(journal=None, warehouse=None,
                          verify_ratio=0.5, verify_min=1)
    _ = engine._should_verify()  # no exception


def test_run_one_probe_journal():
    from data.local.pipeline_journal import PipelineJournal
    from data.local.warehouse import LocalDataWarehouse
    import pandas as pd

    journal = PipelineJournal()
    wh = LocalDataWarehouse()
    engine = StreamEngine(journal=journal, warehouse=wh, n_workers=1)
    run_id = journal.start_run("test")

    def mock_fetch(sym):
        return pd.DataFrame({
            "date": ["2026-06-26"], "symbol": [sym],
            "open": [10.0], "high": [11.0], "low": [9.0],
            "close": [10.5], "volume": [10000], "amount": [105000],
        })

    engine._run_one(run_id, "000001", "2026-06-26", mock_fetch, verify_fn=None)
    summary = journal.get_run_summary(run_id)
    assert summary["total"] >= 3
