> **DEPRECATED**: 旧版 V2 多因子系统已注销。L4 架构使用 `backtest/l4_fractal_backtest.py`（形态特征 + 缠论买卖点）。
> 此文档仅作历史参考，所有 V2 相关代码已不再可用。

# 代码实际行为 vs 设计V2 需求对照

---

## L4 版需求（2026-05-12 新架构）

### 架构总览

```
每周五收盘后:
  ┌─ L1 ────────────────────────────────────┐
  │  取消（始终通过）                        │
  └──────────────────────────────────────────┘
        │
        ▼
  ┌─ L2: 板块排名 ──────────────────────────┐
  │  1. 所有ETF/概念板块计算涨停热度         │
  │     (近5日涨停数, 区分10%/20%)           │
  │  2. 所有板块计算趋势评分                 │
  │  3. 涨停热度×60% + 趋势强度×40%         │
  │  4. 排名取前5 → 强势板块                │
  └──────────────────────────────────────────┘
        │
        ▼
  ┌─ L3: 形态识别 ──────────────────────────┐
  │  遍历强势板块内的所有个股:               │
  │  1. 形态特征检测(涨停/平台/W底/旗形/    │
  │     均线粘合, 任一即可)                  │
  │  2. 缠论买卖点检测(一二三类)             │
  │  形态+缠论双通过 → 形态合格池            │
  └──────────────────────────────────────────┘
        │
        ▼
  ┌─ L4: 财务筛选 ──────────────────────────┐
  │  对形态合格池的个股:                     │
  │  1. 拉取营收同比 & 现金流数据            │
  │  2. 营收同比 < -30% → 剔除              │
  │  3. 现金流净额 <= 0 → 过滤掉            │
  │  通过 → 最终L4交易池(≤10只)             │
  └──────────────────────────────────────────┘
        │
        ▼
  ┌─ 交易执行 ───────────────────────────────┐
  │  持有期内:                               │
  │  - 顶分型确认 → 确认K线最低价卖出        │
  │  - 财务不合格 → 立即卖出                 │
  │  - L2板块跌出前五 → 提醒不减仓           │
  │  等权持有, 不满10只留现金                │
  └──────────────────────────────────────────┘
```

### L1 — 大盘环境

| 项目 | 规则 |
|------|------|
| 状态 | **取消**，始终通过 |
| 仓位限制 | 无（100%可用） |

### L2 — 板块排名

| 项目 | 规则 |
|------|------|
| 范围 | 现有13个ETF + 概念板块 |
| **涨停热度(60%)** | 近5个交易日板块内个股涨停累计数 |
| 涨停阈值 | 600/601/603/605/000/001/002/003 = 10%; 300 = 20% |
| 排除范围 | 科创板(688) / 北交所(4/8开头) |
| 交叉归属 | 跨板块涨停都计 |
| **趋势强度(40%)** | 复用现有 `assess_sectors` 趋势评分 |
| 评估频率 | **每周五**收盘后排名 |
| 输出 | 强势板块 **Top 5** |

### L3 — 形态筛选

| 项目 | 规则 |
|------|------|
| 范围 | 强势板块内的所有个股 |
| **形态检测** | 涨停突破 / 平台突破 / W底 / 旗形突破 / 均线粘合（任一即可） |
| **缠论买卖点** | 一买/二买/三买 / 一卖/二卖/三卖（至少一个） |
| 有形态无买卖点 | → **观察不入池** |
| 隐形约束 | 上市≥1年、非ST、流动性达标、流通市值≥30亿 |
| 代码前缀排除 | 688(科创板) / 4/8开头(北交所/老三板) |
| 输出 | **形态合格池**（量化日报用，全部展示） |

### L4 — 财务筛选

| 项目 | 规则 |
|------|------|
| 范围 | 形态合格池的股票 |
| **营收同比** | < -30% → **剔除** |
| **现金流(经营)** | 最近一期净额 ≤ 0 → **过滤掉** |
| 财报时效 | 按报送日期取最新报告期（年报/一季报/中报/三季报） |
| 数据源 | `ak.stock_yjbb_em`（营收同比）+ `ak.stock_cash_flow_rm`（现金流） |
| 预拉取范围 | **只拉形态合格池的股票**，不预先全量拉取 |
| 营业利润同比 | 有数据就展示（日报用），不作为硬过滤条件 |
| 输出 | **最终L4交易池(≤10只)** |
| 量化日报 | 同时展示: (1)形态合格池全量 (2)L4交易池(≤10只) |

### 交易执行

| 项目 | 规则 |
|------|------|
| **买入** | 底分型确认后 → 确认K线**收盘价** + 0.75%成本(佣金0.25%+滑点0.5%) |
| **卖出** | 顶分型确认 → **必须卖出**，确认K线**最低价** - 0.835%成本(印花0.085%+佣金0.25%+滑点0.5%) |
| **强制卖出** | 财务不合格 → 立即卖出 |
| L2变化 | 板块跌出前五 → **提醒风险/减仓**，不强制卖出 |
| 持仓权重 | **等权**（最多10只） |
| 资金使用率 | 不足10只留现金 |
| 信号延迟 | 所有信号后移一根K线确认，**无未来函数** |
| 分型识别 | `identify_fractals` + `filter_valid_fractals`，采用确认K线机制 |

### 数据缓存方案

| 数据类型 | 来源 | 缓存方式 |
|---------|------|---------|
| 涨停统计 | 现有日线数据自算（按代码前缀区分10%/20%） | 不需要额外拉取 |
| 营收同比 | `ak.stock_yjbb_em()` 按月份拉取 | `data/raw/_cache/financial/yjbb_{YYYYMM}.parquet` |
| 经营现金流 | `ak.stock_cash_flow_rm()` 按股票拉取 | `data/raw/_cache/financial/cf_{symbol}.parquet` |
| 历史缓存 | 回测前删除重建，确保口径一致 | 所有旧 parquet 缓存清空后重跑 |

### 回测参数

| 项目 | 值 |
|------|------|
| 周期 | 2019-01-01 至 2026-05-08 |
| 范围 | 强势板块及强势板块下的个股，符合条件的入L4交易池 |
| 运行 | 全程静默，仅输出年度结果 |
| 输出 | 年度收益/夏普/回撤/交易统计/按年明细 |

<!-- ===== 以下为原 V2 版需求文档（保留作参考） ===== -->

> 基于 2026-05-10 代码现状反推，对照 `docs/factor-design-v2.md`（V2.12）
> 
> 最近一次更新：修复 D1-D6(全部6项)、H1-H8(8项)、§9(阴跌L3暂停)、§14(独立强势股清理)、§15(L2 TopK联动)、H4(形态乘数独立cap)。经确认保留代码：(D4 passed语义)。

---

## 1. 总体架构

| 维度 | 设计 V2 | 代码实际 | 判定 |
|------|---------|---------|------|
| 系统架构 | 三层漏斗 + 循环等待 + 技术资金双验证 | 三层漏斗 + 循环等待 + 技术资金双验证 | ✅ 一致 |
| 入口点 | gate/engine/ 新架构 | gate/engine/ 新架构已实现，但旧 models/ 入口点 (`main.py`/`backtest_cross_section.py`) 仍存在 | ⚠️ 新旧并存 |
| 运行节奏 | L1/L2 周频，L3 日频 | L1/L2 周频，L3 日频 | ✅ 一致 |
| 状态机 | IDLE→COOLING→SCANNING→HUNTING→HOLDING | 同设计 5 状态 | ✅ 一致 |
| 前置过滤 | ST/退市/次新股/流动性/连续跌停 | 5 项全部实现（但 60 天用交易日近似） | ⚠️ 基本一致 |

### 状态转移表

| 当前状态 | 可转移至 | 触发条件 |
|---------|---------|---------|
| IDLE | SCANNING | 系统启动 / 周度评估日到来 + L1 gate 通过 |
| IDLE | COOLING | 系统启动时检测到刚清仓（H7 修复后） |
| SCANNING | HUNTING | L1 gate 通过 + L2 候选池非空 |
| SCANNING | IDLE | L1 gate 失败（见 gate 失败路径分流） |
| HUNTING | HOLDING | L3 选股成功，有持仓 |
| HUNTING | IDLE | L3 无候选 / L2 候选池为 0 |
| HOLDING | HUNTING | 新周度评估日 gate 通过，重新选股调仓 |
| HOLDING | IDLE | 全部清仓 / gate 失败 |
| HOLDING | COOLING | 主动/被动清仓（触发冷却期） |
| COOLING | SCANNING | COOLING 期满（4 周）+ L1 gate 通过 |
| COOLING | IDLE | COOLING 期满但 gate 未通过 |

> COOLING 时长 = 4 周，纳入 `config/settings.py` 中的 `STATE_COOLING_WEEKS` 配置项。
> COOLING 期间仅运行 L1/L2 评估，不运行 L3 选股。

### 部分数据缺失降级

| 缺失率 | 行为 | 加权系数 |
|--------|------|---------|
| 缺失率 ≤ 30% | 跳过缺失标的，正常评估其余 | 1.0 |
| 缺失率 30%-50% | L3 降级（量价替代资金面） | 0.9 |
| 缺失率 > 50% | L4 降级（纯技术面），输出告警 | 0.8 |

> 单只 ETF / 个股数据缺失：跳过该标的，不影响同层其他标的评估。
> 实现位置：`gate/layer2_sector.py` 和 `gate/layer3_stock.py` 入口处统一处理。

---

## 2. 第一层：大盘环境（周线评估）

### 2.1 门卫逻辑 — 已确认：保留 2-of-3

| 维度 | 设计 V2 | 代码实际（已确认） |
|------|---------|---------|
| **判定方式** | AND 硬门槛：3 个技术面条件全部通过 | **2-of-3 计分制**：3 个条件满足 ≥2 即可 ✅ |
| 条件 B1 | 周线MACD: DIF>DEA 且柱状图为正 | 周线金叉近8周出现 ✅ |
| 条件 B2 | 月线MACD: 金叉 OR 底背离+DIF拐头 OR DIF零轴上 | 月线金叉 OR DIF>0 |
| 条件 B3 | 无周线顶背驰（作为 AND 条件之一）| 周线底背驰（作为底部共振条件）|
| **看空拦截** | 无明确提及 | 看空加权分 ≥ 4（顶背驰×3 + 周线死叉×2 + 月线死叉×2）+ 阴跌检测 |

**判定逻辑（已确认）**：

```
bullish_score ≥ 2 AND bearish_weighted < 4 → 通过
```

> **注意**：阴跌检测已从 gate 条件中移除。阴跌时 gate 正常判定，但下游设置 `l3_suspended = True`，L1/L2 照常运行、L3 暂停（详见 §9）。

**Gate 失败路径分流**：

| 失败条件 | 市场状态 | 仓位 | L2 | L3 | 说明 |
|---------|---------|------|-----|-----|------|
| `bearish_weighted ≥ 4` | 强制熊市 | 0% | 不运行 | 不运行 | 严重看空，全停 |
| `bullish_score < 2` | 按 strong_count 降级 | 0%~30% | 正常运行 | 正常运行 | 看多不足但非严重看空 |
| 阴跌触发 | 按正常判定 | 正常×折扣 | 正常运行 | **暂停** | 见 §9 |

### 2.2 资金面

| 维度 | 设计 V2 | 代码实际 | 判定 |
|------|---------|---------|------|
| 角色 | 确认器（不参与 AND 硬门槛）| 确认器 | ✅ 一致 |
| L1 数据源 | 北向汇总 + 成交额趋势 | 北向汇总 + 成交额趋势 | ✅ 一致 |
| L2 降级 | 仅北向汇总，×0.85 | 仅北向，×0.85 | ✅ 一致 |
| L3 退化 | 仅成交额量价关系，×0.85 | 量价替代，×0.85 | ✅ 一致 |
| L4 兜底 | 纯技术面，×0.8 | 纯技术面，×0.8 | ✅ 一致 |
| 分歧处理 | 资金与技术反向 → 不通过 | 北向净流出 → 不通过 | ✅ 一致 |

### 2.3 三市场综合判定

| 维度 | 设计 V2 | 代码实际（已确认） | 判定 |
|------|---------|---------|------|
| 3 强 | 牛市 | 牛市（要求 ≥2 市场有底部共振）| ⚠️ 增加了底部共振条件 |
| 3 强（<2 底部共振） | — | 震荡（仓位 60%）| 🟡 R2 新增：技术面健康但底部结构不足 |
| 2 强 1 弱 | 震荡 | 偏弱 | ✅ 已确认：保留代码"偏弱" |
| 1 强 2 弱 | 偏弱 | 偏弱 | ✅ 一致 |
| 0 强 | 熊市 | 熊市 | ✅ 一致 |
| 创业板仓位抑制 | 无明确提及 | 创业板看空 → 仓位上限 30%（详见 R1） | 🟡 代码新增 |
| 仓位公式 | 基础仓位 × min(三市场可信度) | 基础仓位 × min(可信度) × 风险折扣(×0.7) | ⚠️ 代码多了risk_discount |
| 风险折扣 | 无 | 任一市场有顶部信号 → 仓位×0.7 | 🟡 代码新增 |

### 仓位公式（已确认）

```
final_position = base_position                              // strong_count 决定
               × min(三市场可信度)                           // 每个市场各自资金降级系数
               × risk_discount                               // 0.7 if 任一市场有顶部信号 else 1.0
               × fund_discount                               // L1=1.0 / L2=0.85 / L3=0.85 / L4=0.8
               × bearish_override                            // bearish_weighted ≥ 4 → 0 else 1.0

上限 cap = 100%，下限 floor = 0%
```

> 折扣优先级：bearish_override > risk_discount > fund_discount > 可信度。冲突叠加取更低值（降级+预警→取更低仓位）。

---

## 3. 第二层：ETF分类指数（周线评估）

### 3.1 门卫逻辑 — 已确认：保留 2-of-5

| 维度 | 设计 V2 | 代码实际（已确认） |
|------|---------|---------|
| **判定方式** | AND 硬门槛：5 个条件全部通过 | **2-of-5 计分制**：5 个条件满足 ≥2 即可（牛市≥3，偏弱≥1）✅ |
| 条件 B1 | 周线MACD: 金叉 | DIF>DEA 且 柱状图为正 ✅ |
| 条件 B2 | 月线MACD: 金叉 OR 底背离+DIF拐头 OR DIF零轴上 | 4 个子条件取 ≥2（金叉/零轴上/拐头/底背离）|
| 条件 B3 | 背驰: 无顶背驰 | 周线底背驰（作为底部共振条件）|
| 条件 B4 | 相对大盘Alpha: 跑赢沪深300 | 跑赢基准（沪深300），无基准时默认不通过 ✅ |
| 条件 B5 | 资金面: 不反向流出 | 资金面不反向流出 ✅ |
| **看空拦截** | 无 | 看空加权 ≥ 阈值（bull=3, volatile=4, weak=5）|

**判定逻辑（已确认）**：

```
bullish_score ≥ gate_threshold AND tech_conditions_count ≥ 1 AND bearish_weighted < bearish_threshold → 通过
```

> **R3 约束**：`tech_conditions_count` = B1~B4 中满足的个数（排除 B5 资金面），资金面不能单独通过 gate。gate_threshold 值：bull=3, volatile=2, weak=1。

### 3.2 打分体系

| 维度 | 设计 V2 | 代码实际 | 判定 |
|------|---------|---------|------|
| 趋势强度 | 30（MACD柱/ DIF斜率）| Trend30（5维MACD + 中枢位置加成）| ⚠️ 代码实现更详细 |
| Alpha | 25（跑赢沪深300相对幅度）| Alpha25（多周期百分位排名 1w/4w/13w）| ⚠️ 代码更精细 |
| 量能结构 | 25 | Volume25（三部分法）| ✅ |
| 资金力度 | 20 | Fund20（双路径）| ✅ |

### 3.3 强势指数判定

| 维度 | 设计 V2 | 代码实际 | 判定 |
|------|---------|---------|------|
| 候选池 | 过门 → 进入候选池 | 同上 | ✅ |
| TopK | K = max(3, 候选池×30%) | **已确认改：L1 联动**。牛市 ratio=0.45/min_k=4, 震荡 ratio=0.35/min_k=3, 偏弱 ratio=0.25/min_k=3（详见 §15） | 🟡 已确认改造 |
| L1 联动阈值 | 牛市3-of-5, 震荡2-of-5, 偏弱2-of-5 | 完整 TopK 联动规则见 §15（L2 TopK 联动 L1 市场状态，Canonical） | 🟡 已确认改造 |

> **L2 候选池为 0 时的下游行为**：若 0 个 ETF 通过 L2 gate，L3 不运行，`run_strategy.py` 输出空选股列表。状态机不进入 HUNTING 状态，回到 IDLE 等待下一周度评估日。日报 L3 板块标注"本期无强势分类指数，无个股候选"。

---

## 4. 第三层：个股筛选（日线评估）

### 4.1 门卫逻辑 — 已确认：保留 3 条件（背驰覆盖顶+底）

| 维度 | 设计 V2 | 代码实际（已确认） |
|------|---------|---------|
| **判定方式** | AND 硬门槛：4 个条件全部通过 | **2-of-3 计分制**：3 个条件满足 ≥2 即可 ✅ |
| **条件数量** | 4 个 | 3 个（B1=周线MACD, B2=月线MACD, B3=资金面），背驰检查同时在看空拦截中覆盖顶背驰和底背驰 ✅ |
| 条件 B1 | 周线MACD: 金叉 | DIF>DEA 且 柱状图为正 ✅ |
| 条件 B2 | 月线MACD: 金叉 OR 底背离+DIF拐头 OR DIF零轴上 | 同设计 ✅ |
| 条件 B3 | 背驰: 日线/周线不能有顶背驰 | 资金面不反向流出（背驰在看空拦截中同时覆盖顶背驰和底背驰）✅ |

**判定逻辑（已确认）**：

```
bullish_score ≥ 2 AND bearish_weighted < 4 → 通过
（bearish_weighted 中同时包含顶背驰和底背驰检查）
```

### 4.2 打分体系 — 已确认：保留形态乘数 × 多因子截面评分

| 维度 | 设计 V2 | 代码实际（已确认） |
|------|---------|---------|
| 缠论买点 | 25 分（独立计分）| 形态乘数（0.90x-1.20x）× 多因子截面评分 ✅ |
| 平台突破 | 20 分（独立计分）| 涨停突破 0.3 / 经典形态 0.3 / 缠论买点 0.4 → 合成形态乘数 ✅ |
| 均线粘合 | 20 分（独立计分）| 纳入经典形态子项 ✅ |
| 底部反转 | 15 分（独立计分）| 纳入经典形态子项 ✅ |
| 量能 | 20 分（独立计分）| Volume25（三部分法）✅ |

**评分结构（已确认 — 2026-05-08 D5/H4/H6 修正后）**：
```
trend_raw  = trend_macd_5dim()                       // 不经过 z-score（H6 修复）
alpha_raw  = cross_sectional_percentile              // 多周期百分位排名
risk_raw   = risk_composite                          // 顶背驰/死叉等扣分项
pattern_mult = clip(0.90, 1.20,
      limit_up_breakout_cap(≤0.30)                   // H4: 涨停突破独立 cap
    + classic_pattern_cap(≤0.30)                     // H4: 平台突破/均线粘合/W底/旗形
    + chan_buy_cap(≤0.40)                            // H4: 缠论买点独立 cap
)
final_score = trend_score + alpha_score × pattern_mult + risk_score   // D5：仅 Alpha 享受形态加成
```

### 4.3 个股分类

| 分类 | 设计 V2 | 代码实际 | 判定 |
|------|---------|---------|------|
| 强势 | 硬门槛全过 + 资金无折扣 | score ≥ 50 | 🔴 设计看条件，代码看分数 |
| 一般 | 硬门槛过但资金有折扣 | score < 50 | 🔴 |
| 不符合 | 任意一项不通过 | bearish_weighted ≥ 4 拦截 | ✅ |
| **独立强势股例外** | — | 已删除（`is_independent()` 死代码及配置常量已清理）| ✅ 已清理（详见 §14） |

### 4.4 上游保鲜验证

| 维度 | 设计 V2 | 代码实际 | 判定 |
|------|---------|---------|------|
| L1 保鲜 | 日线MACD金叉 + 未跌破20日线 | `check_exit_signals` 中检测 L1 环境转弱 | ⚠️ 部分实现 |
| L2 保鲜 | 遍历每个强势ETF，检查日线状态 | `check_exit_signals` 中检测 ETF 指数跌出强势池 | ⚠️ 部分实现 |

---

## 5. 退出机制

| 信号 | 设计 V2 | 代码实际 | 判定 |
|------|---------|---------|------|
| 周线MACD死叉 | ✅ | ✅ | ✅ |
| 日线顶背驰 | ✅ | ✅ | ✅ |
| 缠论卖点 | ✅ (Sprint 2) | ✅ | ✅ |
| 资金连续3日流出 | ✅ | ✅ | ✅ |
| 所属指数跌出强势池 | ✅ | ✅ | ✅ |
| L1 环境转弱 | ✅ | ✅（判定标准：strong_count 下降或 market_state 降级） | ✅ |
| L2 失效时减仓 | ✅ | 占位 pass | 🟡 待实现（已纳入 §12.1 H9） |

---

## 6. 设计中有、代码中无的功能

### A. 未实现功能

| 功能 | 设计出处 | 处置 | 状态 |
|------|---------|------|------|
| **累计跌幅追踪** | 讨论中提出 | **已确认不做**（以阴跌检测替代，见 §8） | ❌ 不做 |
| **一票否决（AND 门）** | V2 第 11 行核心思想 | **已确认废弃**（保留 2-of-N，见分歧 1） | ❌ 不做 |
| **动态权重校准** | V2 第 19 行 | **已确认**：月频定期 + 周频监测 + 状态切换立即触发（详见 §8.1） | ✅ 待实现 |
| **多空单调性接入 pipeline** | 3 个离线验证脚本 | **已确认**：接入生产 pipeline，月频/状态切换时自动校准（详见 §8.1） | ✅ 待实现 |
| **仓位升级过渡**（首周50%→次周全量）| V2 仓位过渡章节 | **延后 v2.1**：当前一步到位无实际问题 | 🔵 延后 |
| **仓位冲突叠加**（降级+预警→取更低）| V2 仓位冲突处理 | **已纳入**：完整仓位公式（见 §2.3 仓位公式） | ✅ 已纳入 |
| **基本面数据**（ROE/毛利率/营收增速）| V2 第三层 | **延后 v2.1**：需 ROE/毛利率/营收增速数据源 | 🔵 延后 |
| **中国节假日日历** | — | **延后 v2.1**：当前周末排除可用，后续引入 | 🔵 延后 |
| **efinance 备选数据源** | V2 备选数据源 | **延后 v2.1**：当前 akshare/tushare 可用 | 🔵 延后 |
| **冷却期** | V2 清仓后重新入场 | **已纳入**：COOLING 状态转移规则 + H7 修复（见 §1 状态转移表） | ✅ 已纳入 |
| **跨 ETF 分散选股** | V2 第三层 etf_tags | **延后 v2.1**：需先填充 etf_tags | 🔵 延后 |
| **ETF 强度加成** | V2 第三层 | **延后 v2.1**：冷启动暂缓 | 🔵 延后 |
| **28 申万行业交叉验证** | `docs/三层统一评分架构设计.md` | **延后 v2.1**：需先联动 Gate 系统 | 🔵 延后 |
| **停牌复牌处理** | V2 停牌处理 | **延后 v2.1**：FROZEN 状态已存在，行为待完善 | 🔵 延后 |
| **数据源降级 Level 0-4** | V2 系统级降级 | **延后 v2.1**：数据抽象层存在但被绕过 | 🔵 延后 |

### B. 已实现功能（从设计中来的）

| 功能 | 设计出处 | 状态 |
|------|---------|------|
| **缠论卖点**（一卖/二卖/三卖）| V2 第三层 | ✅ 已实现 |

---

## 7. 代码中有、设计中无的功能

| 功能 | 代码位置 | 说明 |
|------|---------|------|
| **阴跌检测** | `layer1_market.py:124-133` | 12周跌幅 < -10% AND 低于12周MA AND 4周 < -5%。行为定义见 §9 |
| **创业板仓位抑制** | `layer1_market.py:251-274` | 创业板看空 → 仓位上限 30%（通过状态降级实现），详见 R1 |
| **风险折扣 ×0.7** | `layer1_market.py:282` | 任一市场有顶部信号 → 仓位打 7 折 |
| **底部共振额外要求** | `layer1_market.py:257` | 3 强 → 牛市还需要 ≥2 市场有底部分 |
| **L2 动态看空阈值** | `layer2_sector.py:33-35` | `{"bull":3, "volatile":4, "weak":5}` |

---

## 8. 核心分歧确认（2026-05-08 已确认）

| # | 分歧 | 设计 | 代码 | 决定 | 状态 |
|---|------|------|------|------|------|
| **1** | **AND 门 vs 2-of-N** | 三层全部 AND（所有条件都过才通过）| 三层全部 2-of-N（≥2 即可）| **保留代码**：三层全是 2-of-N | ✅ 已确认 |
| **2** | **2 强 1 弱** | 判定为"震荡"（仓位 50%）| 判定为"偏弱"（仓位 30%）| **保留代码**：2 强 1 弱 → 偏弱 | ✅ 已确认 |
| **3** | **L3 条件数量 + 背驰** | 4 个条件（含资金面独立）| 3 个条件（B1=周线MACD, B2=月线MACD, B3=资金面），背驰检查同时包含顶背驰和底背驰 | **保留代码 3 条件**，背驰覆盖顶+底 | ✅ 已确认 |
| **4** | **L3 评分体系** | 缠论买点 25 分 + 形态 55 分 + 量能 20 分 = 独立计分 | 形态作为乘数（0.90x-1.20x），乘在多因子截面评分上 | **保留代码**：形态乘数 × 多因子截面评分 | ✅ 已确认 |
| **5** | **累计跌幅+动态权重** | 讨论中提出 | 累计跌幅完全未实现，动态权重仅有手写规则（按市场状态调整）| **接入数据驱动**：3 个离线验证脚本接入生产 pipeline，`get_dynamic_weights()` 改为基于单调性/回测结果自动校准 | ✅ 已确认 |

---

### 你之前问到的具体特性

- **创业板仓位抑制**: 已确认保留代码 2-of-N 制。AND 门已废弃（分歧 1），代码中仅保留创业板看空对仓位的抑制逻辑（仓位上限 30%，通过状态降级实现），详见 R1。
- **阴跌市场行为**: 详见 §9。
- **累计跌幅**: 不做。阴跌市场中只运行 L1 + L2，跳过 L3 选股
- **动态权重**: 已确认 → `get_dynamic_weights()` 手写规则改为数据驱动自动校准（详见 §8.1）。

---

### 8.1 动态权重校准机制（Canonical）

**触发时机**：

| 触发类型 | 频率 | 说明 |
|---------|------|------|
| 定期校准 | **月频**（每月首个交易日） | 基于最近 12 个月因子单调性 IC + 回测收益归因，自动调整权重 |
| 周频监测 | **周频** | 监控因子 IC 衰减，若单因子 IC < 0.02 持续 4 周 → 告警 |
| 状态切换 | **立即触发** | L1 市场状态切换（牛市↔震荡↔偏弱↔熊市）时立即重新校准 |

**数据来源**：
- `validate_factors.py` — 因子有效性验证（IC/IR 分析）
- `monotonicity_test.py` — 因子单调性测试
- `_monotonicity_test.py` — 补充单调性检验

三个脚本从手动运行 → 接入生产 pipeline，月频/状态切换时自动调用。

**校准方法**：
- 基于因子单调性 IC + 回测收益归因
- 自动调整 `get_dynamic_weights()` 输出的 trend/alpha/risk 三因子权重
- 权重总和 = 100（等比例缩放，修复 D3）
- **阴跌冻结**（D6）：阴跌市场 L3 因子权重冻结，沿用最近一次非阴跌期校准值，只校 L1/L2
- **形态乘数排除**（R4）：形态乘数不纳入自动校准

**实现位置**：`factors/multi_factor.py:get_dynamic_weights()` + `validate_factors.py` + `monotonicity_test.py`

---

## 9. 新需求：阴跌市场 L3 暂停

### 需求

阴跌市场中（当前代码已有阴跌检测：12周跌幅 < -10% AND 低于12周MA AND 4周 < -5%），**只运行 L1 大盘环境评估 + L2 ETF分类指数评估，跳过 L3 个股选股**。

### 逻辑

```
阴跌检测触发 → L1 运行（评估大盘）→ L2 运行（评估指数）→ L3 跳过（不选股）
```

### 阴跌期间持仓规则

现有持仓保留，但每只持仓标记 `⚠ 阴跌警戒`。退出信号照常生效（周线死叉、日线顶背驰、缠论卖点、资金连续 3 日流出等触发止损/止盈）。**禁止新增开仓和加仓**。若阴跌持续超过 **6 周**，触发强制减仓至原仓位的 50%。

### L3 恢复条件

连续 **2 周**不满足阴跌检测三项条件中的任意两项（即"12 周跌幅 >= -10%"不再成立，或"高于 12 周 MA"，或"4 周跌幅 >= -5%"），L3 在第 **3 周**恢复运行。恢复后 L3 因子权重使用最新非阴跌期校准值（见 §8.1 / D6）。

### 相关引用

- 阴跌检测触发条件：§7（`layer1_market.py:124-133`）
- Gate 失败路径分流中的阴跌处理：§2.1
- 动态权重阴跌冻结：§8.1 / D6

### 实现位置

- `gate/layer1_market.py:253-255` — 阴跌检测（任一市场触发）
- `engine/scheduler.py:149-158` — L3 暂停/恢复逻辑
- `engine/scheduler.py:294` — L3 执行前检查 `l3_suspended`
- `engine/state_machine.py:82-84` — `l3_suspended` / `_yin_die_recovery_weeks` 状态字段
- `factors/multi_factor.py:517-521` — 阴跌时冻结权重（改用震荡市中性权重）
- `reports/daily_report_v2.py:54-55,64-65,83-84,161-162,224-225` — 日报展示阴跌状态

### 恢复逻辑

连续 **2 周**不满足阴跌检测三项条件中的任意两项 → 第 **3 周** L3 恢复运行。恢复后 L3 因子权重使用最新非阴跌期校准值（见 §8.1 / D6）。

### 状态

✅ 已实现

---

## 10. 新需求：L3 形态分类标签

### 需求背景

当前 L3 流程：gate（2-of-3）→ 多因子截面评分 × 形态乘数 → 排名输出。通过 gate 的股票混在一起，没有按形态类型做区分标记。

### 需求描述

在 L3 gate 筛选出股票池之后，对池内股票按形态类型打标签：

```
L1 漏斗 → L2 强势分类指数 → L3 gate（2-of-3）→ 股票池
                                                    ↓
                                          按形态类型打标签
                                          （不改变打分规则）
```

### 具体规则

1. **标签来源**：L3 gate 筛选后的股票池内，检测以下形态，有则打上对应标签

   | 形态标签 | 检测来源 |
   |---------|---------|
   | 涨停突破 | `detect_limit_up_breakout()` |
   | 平台突破 | `detect_platform_breakout()` |
   | 均线粘合 | `detect_ma_convergence()` |
   | W底 | `detect_double_bottom()` |
   | 旗形突破 | `detect_flag_triangle_breakout()` |
   | 底背驰 | `detect_bottom_divergence()` |
   | 底分型 | `identify_fractals()` |
   | 缠论买点 | `detect_buy_points()` |

2. **打分不变**：仍按现有 L3 gate 打分规则（多因子截面评分 × 形态乘数），标签仅作为附加属性

3. **一只股票可有多个标签**（如同时满足"平台突破"和"底背驰"）

4. **无形态匹配的股票**标记为"无形态"或归入"其他"

### 用途

- 选股结果可按形态类型分组展示
- 观察不同市场环境下哪种形态表现更好
- 为后续"按形态分类配额"做准备（当前不做配额限制）

### 状态

✅ 已确认 — 待实现

---

## 11. Bug 修复确认（2026-05-08 ECC 审计，6 项打包通过）

| # | 问题 | 位置 | 修复方案 | 状态 |
|---|------|------|---------|------|
| **D1** | 退出信号4死代码 — `consecutive_outflow` 字段不存在于 fund_data | `layer3_stock.py:395` | 在 `assess_fund_for_layer()` 返回值中增加 `consecutive_outflow` 字段，由调用方日频追踪计算 | ✅ 已修复 |
| **D2** | 底背驰信号双路径不一致 — precompute 有效期 1 天 vs multi_factor 有效期 5 天 | `precompute.py:43` vs `divergence.py:159` | 统一为 5 天有效期（与 multi_factor 路径一致） | ✅ 已修复 |
| **D3** | `get_dynamic_weights()` 权重调整后总和≠100（牛市=95, 偏弱=103） | `multi_factor.py:356-369` | 添加 `_normalize_weights_sum()` 运行时归一化 | ✅ 已修复 |
| **D4** | `l1_passed` 双重语义 — scheduler 用 `strong_count>=2`，Layer1Result 用 `actual_position>0` | `scheduler.py:143` vs `layer1_market.py:286` | **保留代码现状**（`passed = actual_position > 0`）。理由：`actual_position > 0` 更敏感的反映了仓位可操作性，震荡市 60%仓位理应视为"通过"。scheduler 中已统一使用 `l1_result.passed`，无实际分歧。**关联**：与 H1 统一定义 `MarketState` 枚举 | ✅ 保留代码 |
| **D5** | 形态乘数对 trend/alpha/risk 三维度无差别乘性加成 | `layer3_stock.py:203-205` | 改为 `final_score = trend_score + alpha_score × pattern_mult + risk_score`，仅 Alpha 维度享受形态加成。**关联**：形态乘数不纳入自动校准（R4） | ✅ 已修复 |
| **D6** | 阴跌时 L3 无数据导致动态权重校准失效 | 需求 §9 + 分歧 5 | 阴跌市场 L3 因子权重冻结（沿用最近一次非阴跌期校准值），只校 L1/L2 | ✅ 已实现 |

---

## 12. ECC 审计发现的其他问题（待讨论）

以下问题已在审计中暴露，待后续讨论确认修复方案。

### 12.1 HIGH 级（8 项，已确认修复）

| # | 问题 | 位置 | 修复方案 | 状态 |
|---|------|------|---------|------|
| H1 | L1 中文状态字符串 vs L2 英文键名不匹配，错误传入静默降级 | `layer1_market.py:257` vs `layer2_sector.py:33` | L2 入口加 `_normalize_state()` 映射函数。**关联**：与 D4 同根（L1/L2 状态协议不一致），建议统一定义 `MarketState` 枚举 | ✅ 已修复 |
| H2 | `check_upstream_freshness` 所有市场数据缺失时仍返回"L1 正常"（假阳性） | `layer3_stock.py:569-577` | 加 `checked_count` 判断，无数据时返回 `l1_ok=False` | ✅ 已修复 |
| H3 | **[L3]** 日线/周线顶背驰合并为一个条件，权重固定=3，不区分单重/双重顶背驰 | `layer3_stock.py:132-136` | 拆分为日线(权重2)+周线(权重3)，可叠加，拦截阈值上调到5 | ✅ 已修复 |
| H4 | 涨停突破单一信号即可封顶 `pattern_multiplier=1.20`，其他形态完全冗余 | `precompute.py:273-279` | 总 cap 从 0.30 提至 0.40，允许多类别贡献（如 A+B → 1.30），各部分独立 cap 互不挤压 | ✅ 已修复 |
| H5 | `detect_ma_convergence` 不验证"持续粘合"，与文档矛盾 | `pattern.py:68-72` | 改为连续计数：连续 ≥15 天粘合才标记 | ✅ 已修复 |
| H6 | `trend_macd_5dim` 返回评分而非原始因子，被 z-score 双重放大 | `multi_factor.py:110` | `trend_macd_5dim` 跳过 z-score 标准化，直接参与加权求和 | ✅ 已修复 |
| H7 | COOLING 状态从 IDLE 进入时不启动冷却期，冷却机制形同虚设 | `state_machine.py:125` | `transition(COOLING)` 前调用 `_start_cooling()` | ✅ 已修复 |
| H8 | `mark_recovery_attempt` 未加锁，字典并发修改风险 | `retry.py:79-80` | `mark_recovery_attempt` 和 `should_attempt_recovery` 加 `with self._lock:` | ✅ 已修复 |
| **H9** | L2 失效时减仓逻辑缺失 — §5 标记为"占位 pass"但此前无任何修复计划覆盖 | `layer3_stock.py` 退出信号检查 | 在退出信号检查中增加"所属 ETF 跌出 L2 TopK 池"触发事件，对持有该 ETF 成分股按权重等比减仓至目标仓位，退出周期对齐 L2 周频 | ⏳ 待验证 |

### 12.2 需求文档逻辑冲突（4 项，已确认，待用户最终裁决）

| # | 问题 | Team 建议 | 状态 |
|---|------|---------|------|
| R1 | 创业板仓位抑制行为边界 — 仅抑制仓位？还是也影响 L2 行业选择？ | **仅抑制仓位（保持现状）**。理由：创业板成分股跨多个 ETF，排除困难且易误伤；仓位已从 70%/100%→30%，风险敞口已大幅缩小。**实现位置**：`gate/layer1_market.py:251-274`（`_check_gem_bearish()` 函数），确认仅抑制仓位，不修改 L2 行为 | ✅ 已确认 |
| R2 | 底部共振 <2 个时的判定降级规则 — 3 强但仅 1 个有底部分，算什么状态？ | **降级为"震荡"（仓位 60%）**。理由：技术面健康但缺乏底部结构确认，有保留的乐观。**实现位置**：`gate/layer1_market.py:assess_market()`（约 256-266 行），新增底部共振计数及 <2→震荡降级分支 | ✅ 已确认 |
| R3 | L2 偏弱 `gate_threshold=1` 过于宽松 — 仅凭资金面一项即可通过？ | **保留 gate_threshold=1，但增加最低技术面约束**。`passed = bullish_score >= 1 AND tech_conditions_count >= 1`（B5 资金面不能单独通过）。**实现位置**：`gate/layer2_sector.py` L2 主评估流程中 gate 判定逻辑（当前为内联代码，待抽取为独立函数），增加 `AND tech_conditions_count >= 1` 约束 | ✅ 已确认 |
| R4 | 形态乘数与动态权重的功能边界 — 形态乘数是否纳入自动校准？ | **形态乘数不纳入校准**。形态乘数是个股级别溢价（人工设定 0.90-1.20），动态权重是因子级别分配（数据驱动）。回测 IC 高会自动反映在因子权重中。**关联**：形态乘数仅乘 Alpha 维度，见 D5。**实现位置**：`gate/layer3_stock.py:203-205` 及 `factors/multi_factor.py:get_dynamic_weights()`，确认形态乘数不传入校准函数 | ✅ 已确认 |

---

## 13. 新需求：量化日报格式重构

### 设计原则

日报按三层漏斗结构组织，每层独立一个板块，层层递进。

---

### 第一部分：L1 大盘环境

**内容**：

| 数据项 | 来源 | 说明 |
|--------|------|------|
| 市场状态 | `Layer1Result.market_state` | 牛市/震荡/偏弱/熊市 |
| 强势市场数 | `Layer1Result.strong_count` | 0-3 |
| 基础仓位 | `Layer1Result.base_position_pct` | 100%/70%/50%/30% |
| 实际仓位 | `Layer1Result.actual_position_pct` | 基础仓位 × 可信度 × 风险折扣 |
| 风险预警 | `Layer1Result.risk_warning` | 是否有顶部信号 |
| 底部共振数 | `Layer1Result.bottom_bullish_markets` | 0-3 |
| 三市平均分 | `Layer1Result.avg_score` | — |

**L1 参照表**（固定展示）：

| 市场状态 | 强势数 | 基础仓位 | 含义 |
|---------|--------|---------|------|
| 牛市 | 3 | 100% | 三市场全面强势 |
| 震荡 | 3（<2底部分）| 60% | 技术面健康但底部共振不足 |
| 偏弱 | 2 | 30% | 两强一弱 |
| 偏弱 | 3（创业板看空）| 30% | 三强但创业板仓位抑制触发 |
| 偏弱 | 1 | 30% | 一强两弱 + 创业板仓位抑制 |
| 熊市 | 0 | 0% | 三市场全面走弱 |

**L1 点评**：自动生成 2-3 句话，覆盖：
- 当前市场状态及仓位建议
- 多空力量对比（看多条件数 vs 看空加权分）
- 是否触发阴跌检测、创业板仓位抑制、风险折扣

---

### 第二部分：L2 分类指数

**内容**：

| 数据项 | 来源 | 说明 |
|--------|------|------|
| 指数名称 | `SectorVerdict.etf_name` | 申万行业 / 概念分类指数 |
| 指数代码 | `SectorVerdict.etf_code` | — |
| 综合评分 | `SectorVerdict.score` | 趋势30 + Alpha25 + 量能25 + 资金20 |
| 分项得分 | `SectorVerdict.trend_score / alpha_score / volume_score / fund_score` | 四维拆解 |
| 门卫状态 | `SectorVerdict.passed_gate` | 是否通过 2-of-5 |
| 是否强势 | `SectorVerdict.is_strong` | 是否入选 TopK |

**排序**：按综合评分从高到低，强势股 TopK 高亮标记

**L2 对 L1 的前瞻预测**：
- 统计强势指数占比趋势（如连续 N 周上升/下降）
- 若强势指数数量连续 2 周下降 → 预警"L1 可能转弱"
- 若强势指数数量连续 2 周上升 → 提示"L1 可能转强"
- 展示强势指数中"突破类 vs 防御类"的比例变化

---

### 第三部分：L3 个股

**内容**（在现有字段基础上增加形态标签）：

| 数据项 | 来源 | 说明 |
|--------|------|------|
| 股票代码 | `StockVerdict.symbol` | — |
| 综合评分 | `StockVerdict.score` | 0-120（含形态乘数后） |
| 分类 | `StockVerdict.classification` | 强势/一般 |
| 缠论买点 | `StockVerdict.chan_buy_point` | 一买/二买/类二买/三买/二三买重合 |
| 缠论买点分 | `StockVerdict.chan_buy_score` | 0-25 |
| 资金等级 | `StockVerdict.fund_level` | L1/L2/L3/L4 |
| **形态标签** ✨ | 新增（完整列表见 §10） | 涨停突破/平台突破/均线粘合/W底/旗形突破/底背驰/底分型/缠论买点 |
| 归属ETF | `StockVerdict.etf_tags` | — |
| **卖出信号** ✨ | 新增 | 触发退出条件的个股标记卖出 |

**排序**：按综合评分从高到低

**卖出提示**（新增）：

对触发出场信号的个股，逐条列出：

| 卖出信号 | 检测来源 | 展示格式 |
|---------|---------|---------|
| 周线MACD死叉 | §5 | `⚠ 000001 平安银行 — 周线MACD死叉，建议清仓` |
| 日线顶背驰 | §5 | `⚠ 000001 平安银行 — 日线顶背驰，建议减仓` |
| 缠论卖点 | §5 | `⚠ 000001 平安银行 — 一卖/二卖/三卖，建议清仓` |
| 资金连续3日流出 | §5 | `⚠ 000001 平安银行 — 资金连续3日净流出，注意风险` |
| 所属指数跌出强势池 | §5 | `⚠ 000001 平安银行 — 归属ETF(银行)跌出TopK强势池` |
| L1 环境转弱 | §5 | `⚠ L1 环境转弱，全体持仓注意减仓` |
| L2 失效 | §5 | `⚠ 000001 平安银行 — 归属ETF(银行)L2失效，建议减仓` |

每只个股可同时触发多条卖出信号。无卖出信号的个股不展示此项。

**其他保留**：
- 预警信息（原有）
- 推送摘要（原有）
- PDF 输出格式（原有 `reports/daily_report.py`，增加形态标签列 + 卖出信号列）

---

### Team 建议（6 项全部采纳）

#### 建议 1：增加"变动追踪"

日报对比上一期，标注变化：

| 层级 | 追踪项 | 示例 |
|------|--------|------|
| L1 | 状态持续周期 | "偏弱已持续 5 周" |
| L2 | 指数排名变化 | "证券 ↑2 位 / 银行 ↓1 位 / 医药 新进" |
| L3 | 个股评分变化 | "000001 评分 +5.2 vs 上期" |

#### 建议 2：日报头部"状态摘要栏"

在三个部分之前，一条紧凑摘要行：

```
📊 量化日报 2026-05-08（周五 · 周度评估日）
状态: 偏弱 | 仓位: 30% | 强势指数: 3/28 | 候选个股: 12 | 持仓: 2/5
阴跌检测: 未触发 | 资金等级: L2(0.85)
```

#### 建议 3：L2 行业轮动趋势热力图

```
行业趋势（近4周）:
证券    ████████░░ 上升
银行    ██████░░░░ 持平
医药    ████░░░░░░ 上升（新进）
白酒    ██░░░░░░░░ 下降
军工    ░░░░░░░░░░ 走弱
```

#### 建议 4：L3 个股卡片式展示

```
┌─ #1 000001 平安银行 ─────────────────┐
│ 综合评分: 78.5  │ 分类: 强势         │
│ 缠论买点: 二买  │ 买点分: 12         │
│ 形态标签: 平台突破 · 底背驰          │
│ 归属ETF: 银行 (评分72)               │
│ 资金等级: L2(0.85)                   │
└──────────────────────────────────────┘
```

#### 建议 5：区分"推送版"和"完整版"

| 版本 | 用途 | 内容 |
|------|------|------|
| 推送版 | 微信/钉钉推送 | 摘要栏 + L1 点评 + L2 Top5 + L3 Top3 + 预警 |
| 完整版 | PDF 存档 | 全部三层完整数据 + 所有排名 + 评分明细 + 热力图 |

推送版控制在手机一屏内。

#### 建议 6（修改）：申万行业 + 概念指数合并排名

两类指数**合并在一起按评分排名**，增加"类型"列区分：

| 排名 | 指数名称 | 代码 | 类型 | 评分 | 趋势 | Alpha | 量能 | 资金 | 变化 |
|------|---------|------|------|------|------|-------|------|------|------|
| #1 | 证券 | 399975 | 申万行业 | 78.5 | 22 | 20 | 18 | 18.5 | ↑2 |
| #2 | AI | 930713 | 概念 | 75.2 | 20 | 22 | 16 | 17.2 | 新进 |
| #3 | 银行 | 399986 | 申万行业 | 72.1 | 18 | 19 | 17 | 18.1 | ↓1 |

两组独立统计（强势指数中申万 X 个、概念 Y 个），但排名不分开。

---

### 日报最终结构总览

```
┌─────────────────────────────────────────┐
│ 状态摘要栏（建议 2）                     │
├─────────────────────────────────────────┤
│ 第一部分：L1 大盘环境                    │
│  ├─ L1 数值（状态/仓位/风险/底部分/均分）│
│  ├─ L1 参照表                           │
│  ├─ L1 变动追踪（建议 1）               │
│  └─ L1 自动点评                         │
├─────────────────────────────────────────┤
│ 第二部分：L2 分类指数                    │
│  ├─ 合并排名表（申万+概念，含类型列）    │
│  ├─ 分项得分拆解 + 变动追踪（建议 1）   │
│  ├─ 行业轮动热力图（建议 3）            │
│  └─ L2 → L1 前瞻预测                    │
├─────────────────────────────────────────┤
│ 第三部分：L3 个股                        │
│  ├─ 个股卡片（建议 4）                  │
│  ├─ 形态标签 + 变动追踪（建议 1）       │
│  └─ 预警信息 / 退出信号                 │
├─────────────────────────────────────────┤
│ 输出：推送版（建议 5）+ 完整版 PDF      │
└─────────────────────────────────────────┘
```

### 状态

✅ 已确认 — 待实现

---

## 14. 漏斗机制修复：删除独立强势股例外

### 问题

`run_strategy.py:350-358` 的 `is_independent()` 函数是**死代码**：

```python
def is_independent(row):
    chan = row.get("chan_buy_score", 0)  # ← 永远为 0，从未被赋值
    if score >= INDEPENDENT_STOCK_THRESHOLD and chan >= INDEPENDENT_STOCK_CHAN_SCORE:
        ...
```

`chan_buy_score` 字段在整个 pipeline 中没有被任何地方填充，条件永远不满足，独立股路径永远不会触发。

### 决策

**删除独立强势股例外**。理由：
- 当前是死代码，删掉不影响任何行为
- 即使未来要实现，也需要先完成缠论买点评分 pipeline + 历史回测，确认有稳定 Alpha 后再加
- 独立股通道破坏漏斗闭环：一只不属于任何强势板块的股票被选中，L2 过滤形同虚设

### 修复方案

1. ~~删除 `run_strategy.py` 中 `is_independent()` 函数~~（函数已不存在，`run_strategy.py` 已重构）
2. ✅ 删除 `SECTOR_INTEGRATION` 字典中的 `independent_stock_slots`、`independent_stock_score_threshold`、`independent_stock_chan_score` 常量（`config/settings.py:144-147`）
3. `_get_top_stocks()` 中无独立股分支逻辑残留

### 状态

✅ 已清理

---

## 15. 漏斗机制修复：L2 TopK 联动 L1 市场状态

### 问题

`gate/threshold.py:11` 定义的 `default_top_k()`（`layer2_sector.py:526` 调用）是静态值，不随 L1 市场环境变化。牛市和偏弱市场用同一个 K，导致：
- 牛市选股空间充裕但 TopK 没放大，浪费可选标的
- 偏弱市场可选标的少但 TopK 没缩小，弱势板块混入

### 决策

L2 TopK 按 L1 状态动态调整：

| L1 状态 | 选股空间 | TopK 比率 | 最小 K | 逻辑 |
|---------|---------|----------|--------|------|
| 牛市 | 充裕 | 0.45 | 4 | 需要更广的行业覆盖来分散 15 只个股 |
| 震荡 | 正常 | 0.35 | 3 | 适中 |
| 偏弱 | 收窄 | 0.25 | 3 | 聚焦最强势板块，减少噪音 |
| 熊市 | — | — | — | L2 本身不运行（L1 不通过） |

示例：42 个指数过 gate 约 15-20 个：
- 牛市取 0.45 → Top 7-9
- 震荡取 0.35 → Top 5-7
- 偏弱取 0.25 → Top 4-5

### 实现

`gate/threshold.py:12-23` 的 `default_top_k()` 已接收 `l1_state` 参数，通过 `MarketState` 枚举的 `get_top_k_ratio()`/`get_top_k_min_k()` 方法动态计算：

```python
def default_top_k(candidate_count: int, l1_state: str | None = None) -> int:
    state = MarketState.normalize(l1_state) if l1_state else MarketState.VOLATILE
    ratio = get_top_k_ratio(state)
    min_k = get_top_k_min_k(state)
    return max(min_k, int(candidate_count * ratio))
```

`layer2_sector.py:524` 调用时已传入 `l1_market_state`：

```python
k = default_top_k(len(candidates), l1_market_state)
```

L1 状态直接控制 L2 输出宽度 → 传导到 L3 选股池大小 → 形成完整联动漏斗。

### 状态

✅ 已实现


---

## 16. 新需求：因子库 + 权重自动校准

### 概述

建立因子库管理因子生命周期（active/suspended/retired），实现权重按月自动校准、周频偏离检测、因子按周自动调节。

### 16.1 因子库

#### 设计

统一管理的因子注册表，作为因子元信息的单一真相源：

- 持久化于 `factors/factor_library.yaml`
- 11 个初始因子，分为 Trend(趋势)、Alpha(选股)、Risk(风控) 三维
- 每个因子状态：active / suspended / retired
- 记录 suspension_history 供追溯

#### 实现

`factors/factor_library.py` — `FactorLibrary` 类：

- `load()` / `save()` 读写 YAML
- `active_factors` 属性返回当前活跃因子列表
- `suspend_factor()` / `resume_factor()` / `retire_factor()` 管理生命周期
- `update_ic()` 写入因子最新 IC 值
- `run_weekly_regulation()` 周频自动调节（见 16.4）
- 全局单例 `get_factor_library()`

### 16.2 权重自动校准

#### 设计

月频 + 周频双通道校准，覆盖不同时间窗口的需求：

| 触发方式 | 频率 | 管线 | 触发条件 |
|---------|------|------|---------|
| monthly | 月频 | evaluate→calibrate→save | 月末最后一个周五，异步 subprocess 不阻塞主循环 |
| weekly_deviation | 周频 | 同上 | 偏离检测触发（500只采样 × 4周 IC 方向不一致） |
| state_change | 即时 | 同上 | L1 市场状态切换（待实现） |
| manual | 手动 | 同上 | CLI 调用 |

#### 实现

`engine/calibration_runner.py` — `run_calibration()` 管线：

1. 运行 `evaluate_factors.run()` 计算月度 IC
2. 运行 `weights_calibrator.calibrate_weights_by_state()` 按状态计算最优权重
3. 原子替换 `outputs/weights/current_weights_by_state.json`（先写 tmp 再 os.replace）
4. 归档历史权重到 `outputs/weights/history/`
5. 更新 FactorLibrary 中各因子最新 IC 值
6. 写入 `.calibration_done` 标记文件

`scheduler.py` 集成：

- `_maybe_trigger_monthly_calibration()` — 月末周五异步 subprocess 启动
- `_check_pending_calibration()` — 日频检测标记文件，原子替换权重，刷新 multi_factor 缓存

### 16.3 周频偏离检测

#### 设计

不跑全量 4318 只股票（太慢），改为采样 500 只 + 只计算最近 N 周 Rank IC，与当前权重方向对比。

#### 实现

`engine/weight_deviation_detector.py`：

- `compute_recent_ic()` — 500 只采样 × 4 周截面 × 11 因子的 IC 计算
- `detect_weight_deviation()` — 判定偏离规则：
  - weight > 0 但 IC 为负 → direction_mismatch
  - weight ≈ 0 但 IC 为正且较强 → direction_mismatch
  - 任何 critical 偏离 → 触发重新校准
  - ≥3 个 warning 偏离 → 触发重新校准
  - Top3 权重因子 ≥2 个 IC 转负 → 触发重新校准

### 16.4 因子周频自动调节

#### 规则

| 操作 | 条件 | 参数（YAML 可配） |
|------|------|------------------|
| 挂起 | 连续 4 周 IC < -0.01 | consecutive_negative_ic_weeks=4, ic_threshold=-0.01, min_observations=8 |
| 恢复 | 连续 3 周 IC > 0.02，且已挂起 ≥8 周 | consecutive_positive_ic_weeks=3, ic_threshold=0.02, min_suspended_weeks=8 |
| 退役 | 挂起 ≥24 周 | max_suspended_weeks=24 |

#### 实现

`FactorLibrary.run_weekly_regulation()` 执行三条规则，`scheduler._run_weekly_factor_regulation()` 每周五调用。

### 16.5 权重路径动态切换

校准完成后，`multi_factor.py` 需实时加载新权重：

- `set_weights_path(path)` — 校准系统调用设置新路径
- `reload_weights()` — 清除缓存，下次 get_dynamic_weights 从新路径读取
- `_load_state_weights()` — 优先从 _OVERRIDE_WEIGHTS_PATH 加载

### 16.6 日报集成

完整版日报新增第四节「权重校准」：

| 指标 | 数值 |
|------|------|
| 上次校准 | 2026-05-09 |
| 触发原因 | monthly |
| 校准因子数 | 9 |
| 状态区间 | 牛市, 震荡, 偏弱 |

推送版新增一行：`权重校准: monthly | 9因子 | 牛市, 震荡, 偏弱`

因子调节信息独立成行：`因子调节: 挂起2个(volume_reversal,bottom_fractal)`

### 状态

✅ Phase 1-6 全部实现

---

## 17. 新需求：因子库扩建（200+ 因子）

### 概述

将因子库从 11 个手写因子扩展至 166 个注册因子（50 TA-Lib + 73 Alpha158 + 32 Alpha360 + 11 原始），经筛选保留 40-80 个高有效因子进入生产。

### 17.1 因子注册表

#### 设计

中心化因子注册表 `FactorRegistry`，作为所有因子的单一真相源：

- `FactorDef` frozen dataclass：key, library, dimension, category, requires, compute_fn, min_history
- 自动发现：模块通过 `register_all(registry)` 注册因子
- 查询：按 library / dimension / category 分组查询

#### 实现

`factors/factor_registry.py`：

- `FactorRegistry` 单例，`get_registry()` 全局访问
- `register(key, defn)` / `get(key)` / `keys` / `count`
- `keys_by_library()` / `keys_by_dimension(value)` 分组查询
- `summary()` 返回统计摘要
- `to_meta_dicts()` 导出为 FactorLibrary 元数据格式

### 17.2 因子来源

| 来源 | 数量 | 类型 | 实现文件 |
|------|------|------|----------|
| 原始因子 | 11 | 手写计算 | `scripts/evaluate_factors.py`（硬编码 IF-ELSE） |
| TA-Lib | 50 | 技术指标（含降级 stub） | `factors/talib_factors.py` |
| Alpha158 | 73 | 表达式因子（受限 eval） | `factors/alpha158_factors.py` |
| Alpha360 | 32 | 多周期浓缩因子 | `factors/alpha360_factors.py` |

#### TA-Lib（50 因子）

6 类：Overlap(11)、Momentum(28)、Volume(3)、Volatility(4)、Statistics(3)、Pattern(6)

- C 库缺失时优雅降级：全部 50 因子注册为 `lambda **kw: NaN` stub
- Wrapper 模式：`_wrap_single(func)` / `_wrap_hlc(func)` / `_wrap_hlcv(func)` / `_wrap_ohlc(func)`
- 每条 wrapper 内部 try/except → NaN

#### Alpha158（73 因子）

7 组：Kbar(12)、Price(22)、Volume(16)、Rolling(21)、Spread(6)、Range(4)、Trend(6)

- 安全表达式引擎：`_make_expr_fn(expr, requires)` + `_safe_eval(expr, local_vars)`
- 白名单：`_ALLOWED_METHODS`（27 个 pandas 方法）+ `_ALLOWED_NAMES`（8 个标识符）
- `_check_expr_safe(expr)` 逐 token 校验
- `np.*` 函数通过 `np.` 前缀豁免

#### Alpha360（32 因子）

6 组：Price ratio(8)、Vol ratio(4)、VWAP deviation(6)、Range position(5)、Volatility(4)、Price acceleration(4)、Cross(2)

- 复用量产 Alpha158 的 `_make_expr_fn`
- 多周期参数化生成（5/10/20/60 日窗口）

### 17.3 因子分发计算

#### 设计

按 `requires` 字段分组，每组一次性准备 kwargs，逐行调用 compute_fn。

#### 实现

`factors/factor_compute.py`：

- `compute_factors_batch(registry, df, factor_keys)` — 全序列批量计算
- 分组优化：相同 requires 的因子共享数据准备
- `compute_factor_cross_section(registry, df, factor_keys)` — 只返回最后一行

### 17.4 因子正交化

#### 设计

对称正交化（Symmetric Orthogonalization）：通过 `scipy.linalg.sqrtm()` 将因子协方差矩阵开方求逆，消除因子间多重共线性。

#### 实现

`factors/orthogonalize.py`：

```python
cov = (X.cov() + X.cov().T) / 2  # 对称化
eigvals, eigvecs = np.linalg.eigh(cov)
eigvals[eigvals < threshold] = threshold  # 秩缺陷保护
cov_sqrt = sqrtm(eigvecs @ diag(eigvals) @ eigvecs.T)
cov_inv_sqrt = real_part(inv(cov_sqrt))
X_ortho = X @ cov_inv_sqrt
```

- `symmetric_orthogonalize(factor_df, standardize="mad", rank_threshold=1e-10)`
- `orthogonalization_report(raw, ortho)` → 正交前后最大相关系数
- 验证：max correlation from 0.97 → 0.0

### 17.5 Alphalens 适配器

#### 实现

`factors/alphalens_adapter.py`：

- `prepare_factor_data(factor_values, prices, forward_days)` → MultiIndex Series
- `compute_forward_returns(prices, forward_days)` → 多周期前向收益
- `run_analysis(factor_data, prices, forward_days)` → IC/Quantile/Turnover tear sheet
- `batch_analysis(factor_values, prices, forward_days)` → {factor_key: analysis}

### 17.6 因子筛选流水线

#### 设计

综合评分 = 0.35×ICIR + 0.25×胜率 + 0.20×状态稳定性 + 0.20×(1−最大相关性)

#### 实现

`factors/factor_screener.py`：

- `compute_ic(factor_df, forward_returns, method="spearman")` — 逐期 Rank IC
- `run_screening(factor_values, forward_returns, config)` — 完整筛选流水线：
  1. 计算每个因子的 IC/ICIR/胜率
  2. 计算两两最大相关性
  3. 综合评分排序
  4. 取 Top-N

`scripts/screen_factors.py` — CLI 入口：采样 500 股票 → 计算全部因子 → 筛选 → 生成 YAML

### 17.7 因子库 YAML 自动生成

#### 实现

`factors/library_generator.py`：

- `generate_library_from_registry(registry, output_path)` — 从注册表生成完整 YAML
- `generate_from_screening(screening_result, output_path)` — 筛选后生成
- 保留已有生命周期状态（active/suspended/retired）

### 17.8 管线适配

| 文件 | 变更 |
|------|------|
| `factors/factor_library.py` | +bulk_add、+quarterly_re_evaluation、+按维度/类别查询 |
| `factors/multi_factor.py` | +set_orthogonalize、+generate_default_weights、截面正交化集成 |
| `scripts/evaluate_factors.py` | +_compute_via_registry 注册表兜底 |
| `engine/weight_deviation_detector.py` | _compute_single_factor 优先注册表分发 |
| `engine/scheduler.py` | +_maybe_trigger_quarterly_screening 季度再评估 |

### 17.9 性能预算

| 操作 | 11 因子 | 166 因子（注册） | 80 因子（生产筛选后） |
|------|---------|-----------------|----------------------|
| 单股计算 | ~5ms | ~80ms | ~20ms |
| 千股截面 | ~5s | ~80s | ~20s |
| 全量 precompute | ~23min | ~5h（多进程 ~45min） | ~1.5h（多进程 ~12min） |
| 月频校准 | <1s | N/A | ~5s |
| 周频 IC(500 股) | ~2min | N/A | ~5min |
| 因子缓存(内存) | ~40MB | ~1GB | ~300MB |

### 状态

✅ Phase 0-6 全部实现，Phase 7 端到端验证中

### 未完成项

- TA-Lib C 库安装（Windows）：50 个 TA-Lib 因子注册为 stub，返回 NaN。安装方法：
  - `conda install -c conda-forge ta-lib`
  - 或从 https://github.com/cgohlke/talib-build/releases 下载 wheel
- 全量 200 股票 × 166 因子筛选运行（~30 分钟）
- 回测对比：166 因子注册 + 80 因子生产 vs 11 因子基线
-->
