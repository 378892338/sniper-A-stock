# 独立专家代码评审报告

**分支：** `feat/mootdx-stream-engine`
**评审专家：** 3 位独立专家（正确性审计师 + 语言陷阱专家 + 架构评审师）

---

## 确认的缺陷（按严重度排序）

### 🔴 Critical（必须修复）

| # | 文件:行 | 问题 | 场景 |
|---|---------|------|------|
| 1 | `pipeline_journal.py:50` | `_infer_mode()` 首次查询无记录返回"unknown"，所有日志行的 mode 列永久错误 | `start_run("full")` → `log()` → 数据库写入 mode="unknown" |
| 2 | `stream_engine.py:103-106` | VALIDATE 步骤是空操作 — 仅记录时间戳，没有执行任何验证逻辑 | 五步闭环中 validate 永远 pass，掩盖数据质量问题 |
| 3 | `__init__.py:136` | `MootdxRealtimeSource` 注册为 DataSource 但未实现 ABC 接口（无 `is_available`, `fetch_daily`, `name`） | 被加入 `DATA_SOURCE_PREFERENCE` 时会 AttributeError |

### 🟡 Important（应修复）

| # | 文件:行 | 问题 |
|---|---------|------|
| 4 | `stream_engine.py:40-41` | `verify_min`/`verify_max` 被构造函数接收但 `_should_verify()` 未使用 |
| 5 | `settings.py:194` | `INTRADAY_SOURCE_PREFERENCE` 已定义但没有任何代码读取它 |
| 6 | `updater.py:367-369` | 跨源验证中 `val = val_raw` 和 `main = df` 是引用非拷贝，被后续 `.iloc[...]=` 变异 |

### 🔵 Minor（可优化）

| # | 文件:行 | 问题 |
|---|---------|------|
| 7 | `pipeline_journal.py:24` | PipelineJournal 创建独立的 LocalDataWarehouse，与 StreamEngine 持有的实例不同步 |
| 8 | `updater.py:289` | FetcherGuard `_request_count` 在多线程下非原子操作（**pre-existing**） |

---

## 共发现 8 项问题，其中 3 项 Critical、3 项 Important、2 项 Minor

将立即修复 Critical + Important 共 6 项。
