# 📊 量化日报模板

> 本文档展示由 `scripts/daily_report.py` 生成的量化日报结构与字段说明。
> 实际报告由系统每日盘后自动产出，本模板仅用于说明格式与字段含义，不含真实数据。

完整版生成命令：
```bash
python -m scripts.daily_report --date YYYY-MM-DD
python -m scripts.daily_report_html --date YYYY-MM-DD
```

---

## 一、市场状态（L0）

```markdown
## 一、市场状态

- 合成评分: **<L0_COMPOSITE>** <REGIME_ICON> <bullish|neutral|bearish>
- 趋势: <TREND>　量能: <VOLUME>　宽度: <BREADTH>　北向: <NORTHBOUND>
- 仓位缩放: <POSITION_RATIO>　最大持仓: <MAX_POSITIONS>

**L0 周线趋势（近 N 周）**

| 周 | L0均值 | 方向 | 样本 |
|----|:-----:|:----:|:---:|
| ... | ... | ... | ... |
```

**字段说明**：
- `L0_COMPOSITE` — L0 合成评分（0-100，≥60 偏多，≤30 偏空）
- `TREND` / `VOLUME` / `BREADTH` / `NORTHBOUND` — L0 四个子维度
- `POSITION_RATIO` — 当日建议仓位缩放比例

---

## 二、强势板块（Top 3）

```markdown
## 二、强势板块 (Top 3)

- **#1 <SECTOR_NAME>**　综合=<SCORE>　(动量<M> 资金<F> 广度<B> 热度<H>)
- **#2 <SECTOR_NAME>**　综合=<SCORE>　(动量<M> 资金<F> 广度<B> 热度<H>)
- **#3 <SECTOR_NAME>**　综合=<SCORE>　(动量<M> 资金<F> 广度<B> 热度<H>)
```

**字段说明**：板块综合分由动量 / 资金流 / 广度 / 热度四维加权得出。

---

## 三、候选个股

```markdown
## 三、候选个股（N 只）

- <SYMBOL_1>　评分=<SCORE_1>
- <SYMBOL_2>　评分=<SCORE_2>
- ...
```

---

## 四、入场过滤

```markdown
## 四、入场过滤

- 通过: **<PASSED>** 只
  - <SYMBOL>　评分=<SCORE>
- 未通过: <REJECTED> 只

  - <SYMBOL>　<REJECT_REASON>
```

**字段说明**：L3 层按价格、量能、换手率、是否涨停、评分阈值等条件过滤候选个股。

---

## 五、持仓状态

```markdown
## 五、持仓状态

- 总资产: **<TOTAL_VALUE>**　(<TOTAL_RETURN>)
- 现金: <CASH>
- 持仓: **<POSITION_COUNT>** 只　暴露: <EXPOSURE>
- 回撤: <DRAWDOWN>

当日平仓 (N 笔):
  - <SYMBOL>　<PnL_PCT>　<REASON>　持有 <HOLD_DAYS> 天
```

---

## 六、打字机归因

```markdown
## 六、打字机归因

- 市场指纹: [<L0>, <TREND>, <VOLUME>, <BREADTH>]
- 最近邻: <N> 笔　平均 PnL: <AVG_PNL>

| 参数 | 归因值 |
|------|--------|
| stop_loss | <VAL> |
| trailing_stop | <VAL> |
| max_hold_days | <VAL> |
| position_size | <VAL> |
| soft_min_score | <VAL> |
| bullish_threshold | <VAL> |
```

**字段说明**：基于纸带历史交易的最相似邻域，动态归因最优参数。

---

## 七、当前配置参数

```markdown
## 七、当前配置参数

| 模块 | 参数 | 值 |
|------|------|----|
| EXIT | stop_loss | <VAL> |
| EXIT | trailing_stop | <VAL> |
| EXIT | max_hold_days | <VAL> |
| RISK | position_size | <VAL> |
| RISK | max_positions | <VAL> |
| ENTRY | soft_min_score | <VAL> |
| MARKET | bullish_threshold | <VAL> |
| ... | ... | ... |
```

**字段说明**：当前生效的全局参数（由 `sniper/config.py` 定义 + 打字机归因动态调整）。

---

## 八、数据源状态

```markdown
## 八、数据源状态

- **L2 评分**: ✅/⚠️ `<MODE>` — `<SOURCE>`
  - 预计算因子: ✅/⚠️/❌ 最新 `<DATE>`（M 文件，K 只）滞后 N 天
  - 覆盖差距: ✅/⚠️/❌ 因子 X / DB Y，缺 Z 只

| 信号表 | 最新日期 | 滞后 |
|--------|---------|------|
| <SIGNAL> | <DATE> | ⚠️ N 天 |

- 信号正常: <SIGNAL_LIST>
- **日线**: <TOTAL> 只股票 截至 <DATE>
```

**字段说明**：报告数据源健康度、信号新鲜度、覆盖完整性。

---

## 附：完整生成链路

| 阶段 | 命令 | 输出 |
|---|---|---|
| 数据更新 | `python -m scripts.update_data` | 本地 SQLite 增量数据 |
| 预计算因子 | `python -m scripts.precompute_l2` | L2 因子 parquet |
| 盘后流水线 | `python -m scripts.run_postclose` | 触发 L0-L4 + 日报 |
| 日报生成 | `python -m scripts.daily_report --date YYYY-MM-DD` | Markdown 日报 |
| HTML 日报 | `python -m scripts.daily_report_html --date YYYY-MM-DD` | HTML 报告 |
| HTTP 服务 | `python -m scripts.serve_report` | 浏览器访问日报 |

详细架构见 `docs/狙击手架构V3-设计文档.md`。
