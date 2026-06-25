# 018 — akshare API 调用无重试机制

**严重程度**：HIGH  
**审查日期**：2026-05-02  
**状态**：待修复  
**涉及文件**：`data/fetch.py`, `models/sector.py`, `run_daily.py`, `backtest_cross_section.py`, `models/financials.py`

## 问题描述

所有对 akshare 的 HTTP 调用都没有重试逻辑，网络波动一次就导致数据永久丢失。

## 涉及的所有调用点

| 文件 | 行号 | API |
|------|------|-----|
| data/fetch.py | 55 | `ak.stock_zh_a_hist()` |
| data/fetch.py | 78 | `ak.stock_zh_a_hist_tx()` |
| data/fetch.py | 20 | `ak.stock_zh_a_spot_em()` |
| models/sector.py | 41 | `ak.stock_zh_a_hist_tx()` |
| models/sector.py | 68 | `ak.stock_zh_a_hist_tx()` |
| run_daily.py | 129 | `ak.stock_zh_a_hist_tx()` |
| backtest_cross_section.py | 67 | `ak.stock_zh_a_hist_tx()` |
| models/financials.py | 12 | `ak.stock_financial_analysis_indicator()` |

akshare 底层是 HTTP 请求东方财富等数据源，网络波动很常见。当前行为：网络抖一下就返回空数据或抛异常。

另外，没有给 akshare 函数传 `timeout` 参数，极端情况可能无限等待。

## 修复方案

添加通用重试装饰器：

```python
import time
from functools import wraps

def retry_on_failure(max_retries=3, base_delay=1.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    wait = base_delay * (2 ** attempt)
                    logger.warning(f"重试 {attempt+1}/{max_retries}: {func.__name__}({args}) 失败, {wait:.1f}s后重试")
                    time.sleep(wait)
        return wrapper
    return decorator
```

同时对 akshare 调用加上 `timeout=30` 参数。
