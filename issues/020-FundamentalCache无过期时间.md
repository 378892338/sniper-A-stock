# 020 — FundamentalCache 无过期时间，数据可能过时

**严重程度**：HIGH  
**审查日期**：2026-05-02  
**状态**：待修复  
**文件**：`models/financials.py:29-67`

## 问题描述

`FundamentalCache` 是无限期缓存（只判断 key 是否存在，不判断数据新旧）。

```python
class FundamentalCache:
    def __init__(self):
        self._cache: dict[str, dict] = {}  # 永不失效

    def get(self, symbol: str) -> dict:
        if symbol not in self._cache:       # 只看有没有，不看过没过期
            self._cache[symbol] = fetch_financial_indicators(symbol)
            time.sleep(0.15)
        return self._cache[symbol]
```

A 股财报披露周期：
- 一季报：4 月 30 日前
- 半年报：8 月 31 日前
- 三季报：10 月 31 日前
- 年报：次年 4 月 30 日前

如果程序 3 月启动缓存了三季报数据，到 5 月公司已发布年报和一季报，但缓存仍返回旧的三季报数据。

## 影响范围

当前 `FundamentalCache` 主要在 `models/financials.py` 的 `to_factor_scores()` 中使用。如果后续在策略中直接调用，会在财报季过后仍然使用旧数据。

另外 `get_all()` 方法对全部股票池串行请求，800 只 × 0.15s = 120 秒，无并发。

## 修复方案

添加 TTL 过期机制：

```python
class FundamentalCache:
    def __init__(self, ttl_hours: int = 24):
        self._cache: dict[str, tuple[dict, float]] = {}
        self.ttl = ttl_hours * 3600

    def get(self, symbol: str) -> dict:
        now = time.time()
        if symbol in self._cache:
            data, timestamp = self._cache[symbol]
            if now - timestamp < self.ttl:
                return data
        data = fetch_financial_indicators(symbol)
        self._cache[symbol] = (data, now)
        time.sleep(0.15)
        return data
```
