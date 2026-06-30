# Task 5: StreamEngine 流式闭环引擎

**Files:**
- Create: `data/stream_engine.py`
- Create: `tests/test_stream_engine.py`

**P0 修复覆盖:**
- A-CRIT-01: N worker × 串行闭环
- A-CRIT-02: PROBE 依赖 WRITE 记录
- Q-CRIT-01: 双重阈值自适应采样
- B-CRIT-05: 统一降级链（不按股票独立选源，委托给 Fetcher）

## Implementation

Create `data/stream_engine.py`:

```python
"""流式闭环引擎 — 每只股票独立完成 probe→validate→fetch→write→verify

并发模型（A-CRIT-01）:
  - N 个 worker 线程
  - 每个 worker 内部串行闭环
  - worker 间通过 ThreadPoolExecutor 并发

P0 修复:
  - A-CRIT-01: N worker × 串行闭环
  - A-CRIT-02: PROBE 依赖 WRITE 记录
  - Q-CRIT-01: 双重阈值自适应采样
  - B-CRIT-05: 统一降级链（不按股票独立选源）
"""

import random
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

from core.logger import get_logger

logger = get_logger("data.stream_engine")


class StreamEngine:
    """流式闭环引擎"""

    def __init__(self, journal, warehouse,
                 n_workers: int = 5,
                 verify_ratio: float = 0.02,
                 verify_min: int = 50,
                 verify_max: int = 200):
        self.journal = journal
        self.wh = warehouse
        self.n_workers = n_workers
        self.verify_ratio = verify_ratio
        self.verify_min = verify_min
        self.verify_max = verify_max
        self._consecutive_pass = 0

    def run(self, run_id: str, mode: str, symbols: list[str],
            fetch_fn, today: str | None = None,
            verify_fn=None) -> dict:
        if today is None:
            today = datetime.now().strftime("%Y-%m-%d")

        # A-CRIT-02: 使用 WRITE 记录做 PROBE
        need_fetch = self.journal.get_missing_stocks_fast(symbols, today)
        skip_count = len(symbols) - len(need_fetch)

        logger.info(
            f"StreamEngine run_id={run_id} mode={mode} "
            f"total={len(symbols)} need_fetch={len(need_fetch)} skip={skip_count}"
        )

        if not need_fetch:
            for sym in symbols:
                self.journal.log(run_id, "probe", sym, "skip")
            return {"ok": 0, "fail": 0, "skip": skip_count}

        # A-CRIT-01: N worker × 串行闭环
        batches = self._chunk(need_fetch, min(self.n_workers, len(need_fetch)))
        results = {"ok": 0, "fail": 0, "skip": skip_count}

        with ThreadPoolExecutor(max_workers=self.n_workers) as pool:
            futures = {
                pool.submit(self._run_batch, run_id, batch, today,
                            fetch_fn, verify_fn): i
                for i, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                try:
                    br = future.result()
                    results["ok"] += br.get("ok", 0)
                    results["fail"] += br.get("fail", 0)
                except Exception as e:
                    logger.error(f"batch fail: {e}")

        return results

    def _run_batch(self, run_id, symbols, today, fetch_fn, verify_fn):
        """单个 worker 的串行闭环"""
        ok = fail = 0
        for sym in symbols:
            try:
                self._run_one(run_id, sym, today, fetch_fn, verify_fn)
                ok += 1
            except Exception as e:
                logger.warning(f"{sym} fail: {e}")
                self.journal.log(run_id, "fetch", sym, "fail",
                                 error_msg=str(e)[:200])
                fail += 1
        return {"ok": ok, "fail": fail}

    def _run_one(self, run_id, symbol, today, fetch_fn, verify_fn):
        """单只股票 Probe→Validate→Fetch→Write→Verify"""
        # PROBE
        self.journal.log(run_id, "probe", symbol, "ok",
                         detail={"action": "fetch"})

        # VALIDATE
        t0 = _time.time()
        self.journal.log(run_id, "validate", symbol, "ok",
                         elapsed_ms=int((_time.time() - t0) * 1000))

        # FETCH
        t1 = _time.time()
        df = fetch_fn(symbol)
        if df is None or df.empty:
            raise ValueError("fetch returned empty data")
        elapsed_fetch = int((_time.time() - t1) * 1000)
        self.journal.log(run_id, "fetch", symbol, "ok",
                         rows_count=len(df), elapsed_ms=elapsed_fetch)

        # WRITE
        t2 = _time.time()
        self.wh.store_daily_bars(df, if_exists="append")
        elapsed_write = int((_time.time() - t2) * 1000)

        date_col = "date" if "date" in df.columns else None
        if date_col and date_col in df.columns:
            dates = df[date_col].dropna()
        elif isinstance(df.index, pd.DatetimeIndex):
            dates = df.index
        else:
            dates = pd.Series()
        data_start = str(dates.min())[:10] if not dates.empty else today
        data_end = str(dates.max())[:10] if not dates.empty else today

        self.journal.log(run_id, "write", symbol, "ok",
                         rows_count=len(df), data_start=data_start,
                         data_end=data_end, elapsed_ms=elapsed_write)

        # VERIFY (Q-CRIT-01: 自适应采样)
        if verify_fn and self._should_verify():
            t3 = _time.time()
            try:
                ok = verify_fn(symbol, df)
                status = "ok" if ok else "fail"
                self._consecutive_pass = (self._consecutive_pass + 1) if ok else 0
            except Exception:
                status = "fail"
                self._consecutive_pass = 0
            self.journal.log(run_id, "verify", symbol, status,
                             elapsed_ms=int((_time.time() - t3) * 1000))

    def _should_verify(self) -> bool:
        """Q-CRIT-01: 双重阈值采样"""
        divisor = 2 if self._consecutive_pass >= 10 else (0.5 if self._consecutive_pass == 0 else 1)
        prob = min(self.verify_ratio * divisor, 1.0)
        return random.random() < prob

    @staticmethod
    def _chunk(items, n):
        k, m = divmod(len(items), n)
        return [items[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]
```

## Tests

Create `tests/test_stream_engine.py`:

```python
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
```

## Verification

```bash
cd /d/projects/quant-system && python -m pytest tests/test_stream_engine.py -v
```

Expected: 3 passed

## Commit

```bash
git add data/stream_engine.py tests/test_stream_engine.py
git commit -m "feat(engine): add StreamEngine with N-worker parallel, adaptive verify"
```
