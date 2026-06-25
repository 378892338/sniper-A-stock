# 021 — assign_stock_sectors 分类映射过于粗糙

**严重程度**：HIGH  
**审查日期**：2026-05-02  
**状态**：待修复  
**文件**：`models/sector.py:127-141`

## 问题描述

当前 `assign_stock_sectors()` 仅基于股票代码前缀做 4 类粗暴映射，完全不是行业分类：

```python
def assign_stock_sectors(symbol: str) -> list[str]:
    if symbol.startswith("3"):
        return ["399006"]              # 所有3开头 → 创业板指
    if symbol.startswith("688"):
        return ["000688"]              # 所有688 → 科创50
    if symbol.startswith("60") or symbol.startswith("00"):
        return ["000300", "000905"]    # 所有60/00 → 沪深300或中证500
    return ["000905"]                  # 兜底
```

## 具体问题

1. **60xxxx 千股归两类**：茅台(600519)和ST垃圾(600xxx)得到完全相同的分类指数权重
2. **00xxxx 同样问题**：五粮液和几十亿小盘股划为一类
3. **完全没有行业分类**：银行和科技只要代码前缀相同就得到相同分类
4. **cross_index_following 只是事后补救**：通过过去120天的价格相关性来判断跟随度，但相关性不等于行业归属

## 修复方案

使用 akshare 获取股票的实际行业分类：

```python
# 方法1: 申万行业分类
industry = ak.stock_board_industry_cons_em(symbol=code)

# 方法2: 概念板块
concept = ak.stock_board_concept_cons_em(symbol=code)
```

然后建立"行业→对应分类ETF指数"的映射表，将申万行业映射到最相关的ETF分类指数（如：医药生物→医药ETF，计算机→计算机ETF/科技ETF）。
