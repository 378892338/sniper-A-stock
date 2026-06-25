# Task 1 Report: Schema 扩展

**Status:** DONE

## What was implemented

在 `data/local/schema.py` 中新增了 3 张 SQLite 表及 2 个索引：

| 表名 | 常量 | 用途 |
|------|------|------|
| `intraday_snapshot` | `CREATE_INTRA_SNAPSHOT` | 日内快照数据（5 档盘口），含 30 个字段 |
| `pipeline_journal` | `CREATE_PIPELINE_JOURNAL` | 流水线运行日志，含 (symbol, step, status, data_end) 及 (run_id, step) 两个索引 |
| `pipeline_daily_summary` | `CREATE_DAILY_SUMMARY` | 每日数据拉取/校验汇总 |

新增表名常量：`T_INTRA_SNAPSHOT`, `T_PIPELINE_JOURNAL`, `T_DAILY_SUMMARY`

更新 `ALL_DDLS` 列表，追加 5 个条目（3 个表 DDL + 2 个索引 DDL）。

## Verification probe output

```
Tables: [..., 'intraday_snapshot', 'pipeline_daily_summary', 'pipeline_journal', ...]
Indexes on pipeline_journal: ['idx_journal_lookup', 'idx_journal_run']
Schema 验证通过
```

All assertions passed. Tables and indexes created successfully.

## Files changed

- `data/local/schema.py` — 新增 3 个 CREATE TABLE DDL、2 个 CREATE INDEX DDL、3 个表名常量，更新 ALL_DDLS 列表

## Self-review findings

- **Multi-statement DDL 修正**: 原始 task brief 中将 `CREATE_PIPELINE_JOURNAL` 的 CREATE TABLE 与两个 CREATE INDEX 合并为单个 DDL 字符串。但 `warehouse._init_schema()` 使用 `conn.execute()`（仅支持单语句），拆分为 3 个独立 DDL 条目以兼容现有执行模式。
- 现有表结构未受影响，所有旧表持续存在。
- 索引名称使用 `IF NOT EXISTS` 确保幂等。
