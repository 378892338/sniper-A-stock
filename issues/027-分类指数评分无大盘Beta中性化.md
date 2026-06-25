# 027 — 分类指数评分缺少大盘 beta 中性化

**严重程度**：LOW  
**审查日期**：2026-05-02  
**状态**：待修复  
**文件**：`models/sector.py:89-124`

## 问题描述

`calc_sector_strength()` 计算出的分数是绝对值，没有剥离大盘 beta 效应。

在牛市中所有指数分数一起膨胀（因为价格都在涨），在熊市中一起萎缩。而个股评分在 `sieve.py:76-114` 有 `neutralize_factors()` 做市值和行业中性化，分类指数评分也应该有对应的"大盘中性化"处理。

## 影响

- 牛市中 L0 加权对所有股票都正加成，失去了"选强势板块"的区分作用
- 熊市中所有股票都受负加成，且板块间相对强弱被掩盖

## 修复方案

将每个指数的评分减去同期宽基指数（如沪深300）的评分作为 alpha，用 alpha 而非原始分数做 L0 加权：

```python
market_strength = calc_sector_strength(hs300_df)
sector_alpha = sector_strength - market_strength  # 超额强度
```
