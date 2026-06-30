# Task 4 Report: MootdxRealtimeSource

**Status: COMPLETED**

## Files Created
- `data/sources/mootdx_realtime.py` — MootdxRealtimeSource class with IP pool rotation, rate limiting, BJ filter, volume /100, heartbeat keepalive, and active1 suspension detection
- `tests/test_mootdx_realtime.py` — 3 unit tests (no TCP connection needed)

## Files Modified
- `data/sources/__init__.py` — registered `mootdx_realtime` source

## Test Results
```
tests/test_mootdx_realtime.py::test_bj_filter PASSED
tests/test_mootdx_realtime.py::test_ip_pool_nonempty PASSED
tests/test_mootdx_realtime.py::test_rate_limit PASSED
============================== 3 passed in 1.30s ==============================
```

## Commit
- `feee241` feat(mootdx): add MootdxRealtimeSource with IP pool, rate limit, BJ filter, vol/100

## P0 Coverage
| ID | Item | Status |
|----|------|--------|
| P-CRIT-01 | 北交所过滤 (8xxxxx/920xxx) | DONE |
| P-CRIT-02 | batch_size 可配置 | DONE |
| Q-CRIT-04 | volume /100 股->手 | DONE |
| S-CRIT-01 | IP 轮换 + 频率控制 + heartbeat | DONE |
| S-CRIT-04 | active1 停牌检测 | DONE |

## Note
- Added `_BJ_PREFIXES = _BJ_PREFIXES` as class attribute (module-level constant was not accessible via `source._BJ_PREFIXES` in tests). The `fetch_quotes()` method continues to reference the module-level constant directly, so behavior is unchanged.
