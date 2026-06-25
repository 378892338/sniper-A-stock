# 003 symbol 参数丢失，涨停阈值全按主板 10% 处理

**严重程度**: HIGH  
**文件**: `strategies/sieve.py:44`, `models/factors.py:369`  
**类型**: 逻辑错误

## 问题描述

`SieveStrategy.score_single` 调用 `calc_all_factors` 时没有传 `symbol` 参数：

```python
# sieve.py:44
result = calc_all_factors(df_daily, df_weekly, market_ret)
# 缺少 symbol=sym
```

而 `calc_all_factors` 的签名是：
```python
def calc_all_factors(df_daily, df_weekly=None, market_ret=None, symbol=""):
```

`symbol` 默认为空字符串，导致 `detect_platform_limit_up` 中的涨停判断全部走主板 10% 逻辑：

```python
# factors.py:114-115
is_cyb = symbol.startswith("3") or symbol.startswith("688")
limit = 0.20 if is_cyb else 0.10
```

**所有创业板(3开头)和科创板(688开头)股票的涨停检测都用错了阈值（10% 而非 20%）。**

## 修复建议

在 `score_single` 和其调用者 `score_with_sector` 中传递 `symbol`：

```python
# sieve.py score_single 增加 symbol 参数
def score_single(self, df_daily, symbol="", df_weekly=None, market_ret=None):
    result = calc_all_factors(df_daily, df_weekly, market_ret, symbol=symbol)
    ...

# sieve.py score_with_sector 中传递
scores = self.score_single(df_daily, symbol, df_weekly, market_ret)
```

## 影响范围

所有创业板和科创板的"平台内涨停"信号检测（L2 结构层）。
