# 023 — calc_sector_strength 标准化 250 天盲区

**严重程度**：MEDIUM  
**审查日期**：2026-05-02  
**状态**：待修复  
**文件**：`models/sector.py:122-124`

## 问题描述

```python
total = (total - total.rolling(250).min()) / \
        (total.rolling(250).max() - total.rolling(250).min() + 1e-9) * 100
return total.fillna(50)
```

| 时间点 | 行为 | 影响 |
|--------|------|------|
| 第1-249天 | `rolling(250)` 全部 NaN → `fillna(50)` | 所有指数同分 50，无区分度 |
| 第250天 | 第一个有效标准化值 | 从恒定50"断崖式"跳到真实分布 |

## 影响

1. 回测起始的 2019 年前约一整年，所有 34 个指数的 L0 加成完全相同（`50 * 0.4 = 20`），等于没有加权重
2. 第 250 天出现人为的分数断崖，不是市场行为
3. `fillna(50)` 的 50 是拍脑袋的 — 弱于大盘的指数应该是低分而不是被强制拉到 50

## 修复方案

改用 `expanding().min()` / `expanding().max()`，从第 1 天起就能产生有意义的相对评分：

```python
total = (total - total.expanding().min()) / \
        (total.expanding().max() - total.expanding().min() + 1e-9) * 100
return total.fillna(50)  # 仅第一天的NaN需要fill
```
