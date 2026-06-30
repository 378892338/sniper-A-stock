# Task 5: StreamEngine 流式闭环引擎 — 完成报告

## 状态
- **Status**: 完成

## 提交
- **Branch**: `feat/mootdx-stream-engine`
- **Commit**: `5ade5cc` — `feat(engine): add StreamEngine with N-worker parallel, adaptive verify`
- **Files**:
  - `data/stream_engine.py` (176 行)
  - `tests/test_stream_engine.py` (56 行)

## 测试结果
```
tests/test_stream_engine.py::test_chunk PASSED                           [ 33%]
tests/test_stream_engine.py::test_should_verify PASSED                   [ 66%]
tests/test_stream_engine.py::test_run_one_probe_journal PASSED           [100%]

============================== 3 passed in 0.56s ==============================
```

## 实现要点
- **A-CRIT-01**: N worker × 串行闭环 — `ThreadPoolExecutor` + `_chunk` 分片，每个 worker 内部串行执行闭环
- **A-CRIT-02**: PROBE 依赖 WRITE 记录 — `run()` 调用 `journal.get_missing_stocks_fast()` 做 PROBE，基于已有 WRITE 记录决定哪些需要 fetch
- **Q-CRIT-01**: 双重阈值自适应采样 — `_should_verify()` 根据连续通过次数动态调整采样概率
- **B-CRIT-05**: 统一降级链 — `fetch_fn` 作为参数注入，引擎不按股票独立选源
- **测试覆盖**: chunk 分片逻辑、verify 采样无异常、完整 probe→fetch→write→journal 闭环
