# 系统架构 — 设计 vs 实现

> 审查日期: 2026-05-03

---

## 一、设计目标架构 (factor-design-v2.md V2.12)

```
┌─────────────────────────────────────────────────────────────────────┐
│                          ENTRY POINTS                               │
│         main.py  │  run_daily.py  │  backtest_cross_section.py      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      ENGINE (调度 + 状态)                            │
│                                                                     │
│   scheduler.py          state_machine.py                            │
│   ┌─────────────┐       ┌──────────────────────────┐               │
│   │ weekly_cycle │──────▶│ IDLE → COOLING → SCANNING│              │
│   │ daily_cycle  │       │   → HUNTING → HOLDING    │              │
│   └─────────────┘       └──────────────────────────┘               │
│                                                                     │
│   watcher.py            portfolio.py                                │
│   ┌──────────────────┐  ┌──────────────────────┐                   │
│   │ 退出信号检测      │  │ 仓位计算 / FROZEN管理  │                  │
│   │ 停牌/复牌处理     │  │ 降级/升级过渡         │                  │
│   └──────────────────┘  └──────────────────────┘                   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    GATE (三层漏斗门卫)                                │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │                    pre_filter.py                         │       │
│  │         ST过滤 │ 退市过滤 │ 次新股 │ 流动性               │       │
│  └────────────────────────┬────────────────────────────────┘       │
│                           ▼                                         │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │              L1: layer1_market.py (周频)                  │       │
│  │  上证/深证/创业板 ── 周线MACD + 月线MACD + 背驰 + 资金确认  │       │
│  │  输出: 牛/震/偏弱/熊 + 基础仓位 + 可信度系数               │       │
│  └────────────────────────┬────────────────────────────────┘       │
│                           ▼ (L1通过才进入)                          │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │              L2: layer2_sector.py (周频)                  │       │
│  │  ETF分类指数 ── 5门AND: MACD+月线+背驰+Alpha+资金         │       │
│  │  过门后评分: 趋势30 + Alpha25 + 量能25 + 资金20            │       │
│  │  冷启动: Top K = max(3, candidates×30%)                   │       │
│  │  sector_mapper.py: 硬映射→概念补映射→相关性兜底             │       │
│  └────────────────────────┬────────────────────────────────┘       │
│                           ▼ (L2通过才进入)                          │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │              L3: layer3_stock.py (日频)                   │       │
│  │  上游保鲜检查 → 4门AND → 缠论买点+形态+量能评分             │       │
│  │  跨ETF分散选股: 每组Top2 + 去重 + 候补填充                 │       │
│  │  退出信号: 周线死叉/日线顶背驰/连续3日流出/指数跌出/L1恶化   │       │
│  └─────────────────────────────────────────────────────────┘       │
│                                                                     │
│  辅助模块: threshold.py │ fund_fallback.py                          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     FACTORS (因子计算/纯函数)                         │
│                                                                     │
│   macd.py    volume.py    alpha.py    pattern.py                    │
│                                                                     │
│   chanlun/                                                          │
│   ├── contain.py     (K线包含)                                       │
│   ├── fractal.py     (顶底分型)                                      │
│   ├── stroke.py      (笔划分)                                        │
│   ├── divergence.py  (背驰判断)                                      │
│   └── zhongshu.py    (中枢+买卖点)  ← Sprint 2                       │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       DATA (数据层)                                  │
│                                                                     │
│   interfaces.py (DataSource ABC / FundFlowSource ABC)               │
│                                                                     │
│   sources/                                                          │
│   ├── akshare.py   (默认, 全量)                                      │
│   ├── baostock.py  (免费备选, 先实现)                                 │
│   └── tushare.py   (Token备用, 后补)                                  │
│                                                                     │
│   price_data.py    (日/周/月线)     market_broad.py  (大盘+沪深300)    │
│   index_etf.py     (ETF指数)       fund_flow.py     (北向/大单/融资)   │
│   industry.py      (申万行业)       fundamental.py   (ROE/毛利率)     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     SHARED (基础设施)                                 │
│                                                                     │
│   logger.py    cache.py (Parquet+TTL)    retry.py (指数退避)          │
│   calendar.py  (交易日历)                                            │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、实际代码架构 (当前实现)

```
┌═════════════════════════════════════════════════════════════════════┐
║                     ACTUAL ENTRY POINTS                             ║
║                                                                     ║
║  ┌───────────────────────┐  ┌──────────────────────────────┐       ║
║  │      main.py          │  │  backtest_cross_section.py    │       ║
║  │  ⚠ import models.xxx  │  │  ⚠ import models.factors     │       ║
║  │  ⚠ import data.fetch   │  │  ⚠ import models.factors     │       ║
║  │  ✗ 未集成 gate/engine/ │  │  ✗ 未集成 gate/engine/      │       ║
║  └───────────────────────┘  └──────────────────────────────┘       ║
║                                                                     ║
║  ┌───────────────────────┐  ┌──────────────────────────────┐       ║
║  │    run_daily.py        │  │   backtest_600251.py         │       ║
║  │  ✅ strategies.sieve   │  │  ⚠ import models.factors     │       ║
║  │  ⚠ 未集成 gate/engine/ │  │  ✗ 未集成 gate/engine/      │       ║
║  └───────────────────────┘  └──────────────────────────────┘       ║
╚═════════════════════════════════════════════════════════════════════╝
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────────┐
│  OLD PATH (活跃)  │ │  NEW PATH (孤立)  │ │    BACKTEST (混合)        │
│                  │ │                  │ │                          │
│  models/         │ │  gate/           │ │  backtest/               │
│  ├─ factors.py   │ │  ├─ layer1_*.py  │ │  ├─ run.py               │
│  │  (旧因子计算)  │ │  │  ✅ 已实现     │ │  │  ⚠ 调用 score_single() │
│  │               │ │  ├─ layer2_*.py  │ │  │  ✗ 换手率bug未修       │
│  ├─ sector.py    │ │  │  ✅ 已实现     │ │  │  ✗ L0未集成           │
│  │  (7 issues)   │ │  ├─ layer3_*.py  │ │  ├─ runner.py            │
│  │               │ │  │  ✅ 已实现     │ │  │  (简化验证, 无成本)    │
│  ├─ financials.py│ │  ├─ pre_filter   │ │  ├─ auto_tune.py         │
│  │  (死代码)     │ │  │  ✅ 已实现     │ │  │  (独立回测逻辑)        │
│  │               │ │  └─ ...          │ │  └─ ...                  │
│  └─              │ │                  │ │                          │
│                  │ │  engine/         │ │  strategies/             │
│  data/           │ │  ├─ state_machine│ │  └─ sieve.py             │
│  ├─ fetch.py     │ │  │  ✅ 已实现     │ │     ✅ score_single      │
│  │  (旧数据获取)  │ │  ├─ portfolio    │ │     ✅ score_with_sector │
│  │               │ │  │  ⚠ 部分实现    │ │     ✅ neutralize        │
│  └─              │ │  ├─ watcher      │ │                          │
│                  │ │  │  ⚠ 部分实现    │ │                          │
│  ⚠ 两个活跃入口点  │ │  ├─ scheduler    │ │                          │
│    直接依赖此路径   │ │  │  ⚠ _portfolio │ │                          │
│                  │ │  │   未初始化     │ │                          │
│                  │ │  └─              │ │                          │
│                  │ │                  │ │                          │
│                  │ │  factors/        │ │                          │
│                  │ │  ├─ macd.py ✅   │ │                          │
│                  │ │  ├─ volume.py ⚠  │ │                          │
│                  │ │  ├─ pattern.py ✗ │ │                          │
│                  │ │  ├─ alpha.py ✅  │ │                          │
│                  │ │  └─ chanlun/ ⚠   │ │                          │
│                  │ │                  │ │                          │
│                  │ │  data/           │ │                          │
│                  │ │  ├─ interfaces   │ │                          │
│                  │ │  ├─ sources/     │ │                          │
│                  │ │  │  ⚠ baostock   │ │                          │
│                  │ │  │   未注册       │ │                          │
│                  │ │  ├─ price_data   │ │                          │
│                  │ │  ├─ market_broad │ │                          │
│                  │ │  ├─ index_etf    │ │                          │
│                  │ │  ├─ fund_flow    │ │                          │
│                  │ │  └─ industry     │ │                          │
│                  │ │                  │ │                          │
│                  │ │  ⚠ 无入口点调用    │ │                          │
│                  │ │  ⚠ ran_daily.py   │ │                          │
│                  │ │    部分用strategies│ │                         │
└──────────────────┘ └──────────────────┘ └──────────────────────────┘
```

---

## 三、数据流实际路径

```
入口点调用链:

main.py:
  data/fetch.py → models/factors.py → backtest/run.py (score_single)
  └─ 全程旧架构, 不经过 gate/ 或 engine/

backtest_cross_section.py:
  models/factors.py → models/sector.py
  └─ 全程旧架构

run_daily.py:
  data/fetch.py → strategies/sieve.py (内部调用 models/factors.py)
  └─ 半新半旧, 不经过 gate/ 或 engine/

backtest_600251.py:
  models/factors.py
  └─ 全程旧架构
```

```
新架构模块 (gate/ + engine/) 调用链:

  gate/layer1_market.py ──调用──▶ factors/macd.py
                          ──调用──▶ factors/chanlun/divergence.py
                          ──调用──▶ gate/fund_fallback.py

  gate/layer2_sector.py ──调用──▶ factors/macd.py
                          ──调用──▶ factors/alpha.py
                          ──调用──▶ gate/sector_mapper.py
                          ──调用──▶ gate/threshold.py

  gate/layer3_stock.py ──调用──▶ factors/macd.py
                          ──调用──▶ factors/pattern.py
                          ──调用──▶ factors/volume.py
                          ──调用──▶ factors/chanlun/zhongshu.py

  engine/state_machine.py ──不调用──▶ gate/ (仅被动接收结果)

  engine/scheduler.py ──调用──▶ gate/layer1 (assess_market, daily_alert_check)
                        ──调用──▶ gate/layer2 (assess_sectors, check_daily_alert)
                        ──调用──▶ gate/layer3 (assess_stock, check_upstream_freshness)
                        ──调用──▶ engine/watcher
                        
  ⚠ scheduler.py 未初始化 self._portfolio → 降仓逻辑静默跳过
```

---

## 四、模块完成度矩阵

```
                    设计有  代码有  测试有  入口点调用  Issues
                    ─────  ─────  ─────  ────────  ──────
gate/
  layer1_market.py   ✅     ✅     ✅      scheduler   H1
  layer2_sector.py   ✅     ✅     ✅      scheduler   H1
  layer3_stock.py    ✅     ✅     ✅      scheduler   H1,H8
  pre_filter.py      ✅     ✅     ✅      ✗ 无调用    H7
  sector_mapper.py   ✅     ✅     ✅      layer2      -
  threshold.py       ✅     ✅     ✅      layer2      -
  fund_fallback.py   ✅     ✅     ✅      L1,L2,L3    H2

engine/
  state_machine.py   ✅     ✅     ✅      scheduler   M
  scheduler.py       ✅     ✅     ✗      无入口点    H3
  portfolio.py       ✅     ⚠     ✅      ✗ 无调用    H13,M2,M3
  watcher.py         ✅     ⚠     ✅      scheduler   H14,M13

factors/
  macd.py            ✅     ✅     ✅      gate+/old   M17
  volume.py          ✅     ⚠     ✅      L3          M14
  alpha.py           ✅     ✅     ✅(内)  L2          -
  pattern.py         ✅     ✗     ✅(内)  L3          C1,M15
  chanlun/contain    ✅     ✅     ✅      zhongshu    -
  chanlun/fractal    ✅     ✅     ✅      stroke      -
  chanlun/stroke     ✅     ✅     ✅      zhongshu    -
  chanlun/divergence ✅     ✅     -      gate+       M17
  chanlun/zhongshu   ✅     ⚠     ✅      L3          C2,M16

data/
  interfaces.py      ✅     ✅     -       sources     -
  sources/akshare    ✅     ✅     -       factory     M7,M19
  sources/baostock   ✅     ✅     -       ✗ 未注册    H10
  sources/tushare    ✅     ✅     -       factory     L2
  price_data.py      ✅     ✅     -       scheduler   H9(缓存bug)
  market_broad.py    ✅     ✅     -       scheduler   H9(缓存bug)
  index_etf.py       ✅     ⚠     -       scheduler   M8,M9
  fund_flow.py       ✅     ⚠     -       scheduler   缺大单/融资
  industry.py        ✅     ✅     -       mapper      H11
  fetch.py (旧)      -      ✅     -       main/run_daily  M5
  fundamental.py     ✅     ✗     -       -           M1(缺失)

models/ (旧)
  factors.py         旧     ✅     ✅      main/backtest  004(伪基本面)
  sector.py          旧     ✅     -       backtest   015/019/021/022/023/026/027
  financials.py      旧     ✅     -       ✗ 死代码   004/020/M12

入口点
  main.py            -      ✅     -       -          ✗ 旧架构
  run_daily.py       -      ✅     -       -          ⚠ 半新旧
  backtest_cross      -      ✅     -       -          ✗ 旧架构
  backtest_600251     -      ✅     -       -          ✗ 旧架构

图例: ✅ 完成  ⚠ 部分完成/有bug  ✗ 缺失/严重bug  - 不适用
```

---

## 五、核心问题一句话

> **gate/ + engine/ 是独立城堡，入口点全走旧路。**  
> 新架构代码写了、测试写了，但没有一个入口点调用它。  
> 三层漏斗在实际回测中从未运行过。
