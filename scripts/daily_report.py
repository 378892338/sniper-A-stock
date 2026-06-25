"""量化日报生成 — python -m scripts.daily_report --date 2026-06-03

输出 Markdown 格式日报：
  1. 市场状态 (L0)
  2. 强势板块 (L1)
  3. 候选个股 (L2)
  4. 入场过滤 (L3)
  5. 持仓状态
  6. 打字机归因
  7. 当前配置参数
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt
import pandas as pd
import numpy as np

import sniper.config as cfg
from core.logger import get_logger

logger = get_logger("scripts.daily_report")

# ── 辅助 ──

_REGIME_ICON = {"bullish": "📈", "neutral": "➡️", "bearish": "📉"}
_DIR_ICON = {"up": "↑", "down": "↓", "stable": "→"}


def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def _fmt(v, d=2) -> str:
    if isinstance(v, float):
        return f"{v:.{d}f}"
    return str(v)


# ── 各节生成 ──


def collect_l0_weekly(date: str, weeks: int = 24) -> list[dict]:
    """最近 N 周 L0 周线值（按 ISO 周平均），默认 24 周约 6 个月。"""
    from sniper.layers.l0_market import MarketScorer
    from sniper.data_router import DataRouter

    router = DataRouter()
    scorer = MarketScorer(router)
    trading_days = router.get_trading_days_before(date, weeks * 7)

    weekly_map: dict[tuple[int, int], dict] = {}
    for d in trading_days:
        try:
            dt_obj = dt.datetime.strptime(d, "%Y-%m-%d").date()
            week_key = dt_obj.isocalendar()[:2]
            score = scorer.composite_score(d)
            if week_key not in weekly_map:
                weekly_map[week_key] = {"scores": [], "days": []}
            weekly_map[week_key]["scores"].append(score)
            weekly_map[week_key]["days"].append(d)
        except Exception:
            continue

    result = []
    for wk in sorted(weekly_map.keys()):
        entry = weekly_map[wk]
        avg = sum(entry["scores"]) / len(entry["scores"])
        result.append({
            "week_label": f"{wk[0]}W{wk[1]:02d}",
            "avg_l0": round(avg, 1),
            "daily_l0s": [round(s, 1) for s in entry["scores"]],
            "n_days": len(entry["scores"]),
        })
    return result[-weeks:]


def section_market(date: str) -> str:
    """第一节：市场状态 (L0)"""
    from sniper.layers.l0_market import MarketScorer
    from sniper.data_router import DataRouter

    router = DataRouter()
    scorer = MarketScorer(router)
    l0 = scorer.score_all(date)
    regime = scorer.market_regime(date)
    icon = _REGIME_ICON.get(regime, "❓")

    # L0 周线
    weekly_data = collect_l0_weekly(date)
    weekly_lines = []
    if weekly_data and len(weekly_data) >= 2:
        weekly_lines.append("")
        weekly_lines.append("**L0 周线趋势（近 {} 周）**".format(len(weekly_data)))
        weekly_lines.append("")
        weekly_lines.append("| 周 | L0均值 | 方向 | 样本 |")
        weekly_lines.append("|----|:-----:|:----:|:---:|")
        for i, w in enumerate(weekly_data):
            direction = ""
            if i > 0:
                prev = weekly_data[i - 1]["avg_l0"]
                curr = w["avg_l0"]
                if curr > prev + 0.5:
                    direction = "📈"
                elif curr < prev - 0.5:
                    direction = "📉"
                else:
                    direction = "➡️"
            # 标记本周
            label = f"**{w['week_label']}**" if i == len(weekly_data) - 1 else w["week_label"]
            weekly_lines.append(f"| {label} | {w['avg_l0']:.1f} | {direction} | {w['n_days']}天 |")

        # 底部统计
        vals = [w["avg_l0"] for w in weekly_data]
        current_l0 = vals[-1]
        prev_l0 = vals[-2] if len(vals) >= 2 else current_l0
        direction_icon = "📈" if current_l0 > prev_l0 else ("📉" if current_l0 < prev_l0 else "➡️")

        # 连续趋势判断
        consecutive_up, consecutive_down = 0, 0
        for i in range(len(vals) - 1, max(0, len(vals) - 6), -1):
            if vals[i] > vals[i - 1]:
                consecutive_up += 1
                consecutive_down = 0
            elif vals[i] < vals[i - 1]:
                consecutive_down += 1
                consecutive_up = 0
            else:
                break
        if consecutive_up >= 3:
            trend_desc = f"连续 {consecutive_up} 周上升"
        elif consecutive_down >= 3:
            trend_desc = f"连续 {consecutive_down} 周下降"
        else:
            trend_desc = "震荡"
        avg_all = sum(vals) / len(vals)

        weekly_lines.append("")
        weekly_lines.append(f"> 周线 {direction_icon} **{current_l0:.1f}**（上周 {prev_l0:.1f}）")
        weekly_lines.append(f"> 方向判断: {trend_desc}")
        weekly_lines.append(f"> 近 {len(vals)} 周均值: **{avg_all:.1f}**")
        # 日线 vs 周线
        latest_complete_week = None
        for w in reversed(weekly_data):
            if w["n_days"] >= 4:  # 完整交易周
                latest_complete_week = w
                break
        if latest_complete_week and abs(latest_complete_week["avg_l0"] - l0["composite"]) > 2:
            diff = l0["composite"] - latest_complete_week["avg_l0"]
            diff_icon = "📈" if diff > 0 else "📉"
            weekly_lines.append(f"> 日线 {l0['composite']:.1f} vs 最新完整周线 {latest_complete_week['avg_l0']:.1f} {diff_icon}（{'偏强' if diff > 0 else '偏弱'}）")
        weekly_lines.append("")

    lines = [
        "## 一、市场状态\n",
        f"- 合成评分: **{l0['composite']:.1f}** {icon} {regime}",
        f"- 趋势: {l0['trend']:.1f}　量能: {l0['volume']:.1f}　宽度: {l0['breadth']:.1f}　北向: {l0['northbound']:.1f}",
        f"- 仓位缩放: {l0['composite'] / 100:.0%}　最大持仓: {cfg.RISK.max_positions}",
    ] + weekly_lines + [""]
    return "\n".join(lines)


def section_sectors(date: str) -> str:
    """第二节：强势板块 (L1)"""
    from sniper.layers.l1_sector import SectorScorer
    from sniper.data_router import DataRouter

    router = DataRouter()
    sector = SectorScorer(router)
    df = sector.composite_scores(date)
    if df is None or df.empty:
        return "## 二、强势板块\n\n- 无数据\n"

    top3 = df.head(3)
    lines = ["## 二、强势板块 (Top 3)\n"]
    for _, row in top3.iterrows():
        lines.append(
            f"- **#{int(row['rank'])} {row['industry_name']}**　"
            f"综合={row['composite']:.0f}　"
            f"(动量{row['momentum']:.0f} 资金{row['fund_flow']:.0f} "
            f"广度{row['breadth']:.0f} 热度{row['heat']:.0f})"
        )
    lines.append("")
    return "\n".join(lines)


def section_stocks(date: str, sectors: list[str]) -> tuple[str, list[dict], list[str]]:
    """第三节：候选个股 (L2) + 返回候选列表供 L3 用"""
    from sniper.layers.l2_stock import StockScorer
    from sniper.data_router import DataRouter

    router = DataRouter()
    stock = StockScorer(router)

    # 获取板块排名（用于 L3 过滤）
    sector_scorer = stock._sector_scorer if hasattr(stock, '_sector_scorer') else None
    if sector_scorer is None:
        from sniper.layers.l1_sector import SectorScorer
        sector_scorer = SectorScorer(router)

    sector_ranks = {}
    all_scores = sector_scorer.composite_scores(date)
    if all_scores is not None and not all_scores.empty:
        for _, row in all_scores.iterrows():
            sector_ranks[row["industry_name"]] = int(row["rank"])

    candidates = stock.top_stocks(date, sectors)
    if not candidates:
        return "## 三、候选个股\n\n- 无候选\n", [], list(sector_ranks.keys())

    lines = [f"## 三、候选个股 ({len(candidates)} 只)\n"]
    for c in candidates:
        lines.append(f"- {c['symbol']}　评分={c['score']:.1f}")
    lines.append("")
    return "\n".join(lines), candidates, sector_ranks


def section_entry_filter(candidates: list[dict], date: str,
                         sector_ranks: dict[str, int]) -> str:
    """第四节：入场过滤 (L3)"""
    from sniper.layers.l3_entry import EntryFilter
    from sniper.data_router import DataRouter

    router = DataRouter()
    entry = EntryFilter(router)

    passed = []
    rejected = []
    for c in candidates:
        # 找该股票的板块排名
        rank = 10  # 默认可接受范围外
        for sec, r in sector_ranks.items():
            # 简化处理：用最小板块排名作为该股票排名
            rank = min(rank, r)
        result = entry.filter(c, date, rank)
        if result["entry"]:
            passed.append(result)
        else:
            rejected.append(result)

    lines = ["## 四、入场过滤\n"]
    lines.append(f"- 通过: **{len(passed)}** 只")
    for p in passed:
        lines.append(f"  - {p['symbol']}　评分={p.get('score', '?')}")
    lines.append(f"- 未通过: {len(rejected)} 只\n")

    if rejected:
        for r in rejected[:10]:
            lines.append(f"  - {r['symbol']}　{r['reason']}")
        lines.append("")
    return "\n".join(lines)


def section_portfolio(engine_result: dict) -> str:
    """第五节：持仓状态"""
    dvs = engine_result.get("daily_values", [])
    trades = engine_result.get("trades", [])
    m = engine_result.get("m", {})

    lines = ["## 五、持仓状态\n"]
    if not dvs:
        lines.append("- 无数据\n")
        return "\n".join(lines)

    last = dvs[-1]
    lines.append(f"- 总资产: **{last['total_value']:,.2f}**　({_pct(m.get('total_return', 0))})")
    lines.append(f"- 现金: {last['cash']:,.2f}")
    lines.append(f"- 持仓: **{last['position_count']}** 只　暴露: {last['exposure']:,.2f}")
    lines.append(f"- 回撤: {last['drawdown']:.2%}")

    # 当日完成的交易
    sells = [t for t in trades if t.get("action") == "SELL"]
    if sells:
        lines.append(f"\n当日平仓 ({len(sells)} 笔):")
        for t in sells:
            pnl = t.get("pnl", 0)
            cost = t.get("cost", 0) or 1
            pnl_pct = pnl / cost
            lines.append(
                f"  - {t['symbol']}　{_pct(pnl_pct)}　"
                f"{t.get('reason', '')}　持有 {t.get('hold_days', '?')} 天"
            )
    lines.append("")
    return "\n".join(lines)


def section_attribution(date: str, l0_info: dict) -> str:
    """第六节：打字机归因"""
    cfg.load_paper_tape()
    if cfg._TRADE_PAPER is None or len(cfg._TRADE_PAPER) < 10:
        return "## 六、打字机归因\n\n- 纸带不足 10 笔，跳过\n"

    fp = [l0_info["composite"], l0_info["trend"],
          l0_info["volume"], l0_info["breadth"]]
    neighbors = cfg._find_neighbors(fp)
    if len(neighbors) < 10:
        return "## 六、打字机归因\n\n- 近邻不足 10 笔，跳过\n"

    avg_pnl = float(np.mean([t.get("pnl_pct", 0) for t in neighbors]))
    params = cfg._attribution(neighbors)

    lines = ["## 六、打字机归因\n"]
    lines.append(f"- 市场指纹: [{fp[0]:.1f}, {fp[1]:.1f}, {fp[2]:.1f}, {fp[3]:.1f}]")
    lines.append(f"- 最近邻: {len(neighbors)} 笔　平均 PnL: {_pct(avg_pnl)}")
    lines.append("")
    lines.append("| 参数 | 归因值 |")
    lines.append("|------|--------|")
    for k in ["stop_loss", "trailing_stop", "max_hold_days",
              "position_size", "soft_min_score", "bullish_threshold"]:
        v = params.get(k, "—")
        lines.append(f"| {k} | {_fmt(v) if isinstance(v, (int, float)) else v} |")
    lines.append("")
    return "\n".join(lines)


def section_config() -> str:
    """第七节：当前配置参数"""
    lines = ["## 七、当前配置参数\n"]
    lines.append("| 模块 | 参数 | 值 |")
    lines.append("|------|------|----|")
    for section_name, obj, keys in [
        ("EXIT", cfg.EXIT, ["stop_loss", "trailing_stop", "max_hold_days", "ma_break_below"]),
        ("RISK", cfg.RISK, ["position_size", "max_positions", "max_sector_exposure", "min_hold_days",
                             "active_reduction_l0", "active_reduction_exposure"]),
        ("ENTRY", cfg.ENTRY, ["soft_min_score", "soft_sector_top", "hard_min_price", "hard_min_volume"]),
        ("MARKET", cfg.MARKET, ["bullish_threshold", "bearish_threshold",
                                 "trend_weight", "volume_weight", "breadth_weight", "northbound_weight"]),
    ]:
        for k in keys:
            if hasattr(obj, k):
                v = getattr(obj, k)
                lines.append(f"| {section_name} | {k} | {_fmt(v) if isinstance(v, float) else v} |")
    lines.append("")
    return "\n".join(lines)


def section_data_source(date: str) -> str:
    """第八节：数据源状态 — 评分模式、信号新鲜度、覆盖差距。"""
    from data.freshness import DataFreshnessChecker

    checker = DataFreshnessChecker()
    report = checker.full_report(date)

    _ICON = {"ok": "✅", "warn": "⚠️", "error": "🔴"}

    lines = ["## 八、数据源状态\n"]

    # L2 评分模式
    sm = report.get("scoring_mode", {})
    mode = sm.get("mode", "?")
    mode_icon = _ICON.get("ok" if mode == "precomputed" else "warn", "❓")
    lines.append(f"- **L2 评分**: {mode_icon} `{mode}` — {sm.get('source', '?')}")

    # 预计算因子
    pc = report.get("precomputed", {})
    if pc.get("available"):
        s = pc.get("status", "ok")
        icon = _ICON.get(s, "❓")
        lines.append(f"  - 预计算因子: {icon} 最新 `{pc['latest_date']}` ({pc['total_files']} 文件, {pc['total_stocks']} 只) 滞后 {pc['stale_days']} 天")
    else:
        lines.append(f"  - 预计算因子: ❌ {pc.get('message', 'N/A')}")

    gap = report.get("universe_gap", {})
    g = gap.get("gap_total", 0)
    gap_icon = _ICON.get("ok" if g == 0 else ("warn" if g < 1000 else "error"), "❓")
    lines.append(f"  - 覆盖差距: {gap_icon} 因子 {gap.get('precomputed_count', 0)} / DB {gap.get('live_count', 0)}, 缺 {g} 只")
    gb = gap.get("gap_by_board", {})
    if gb and g > 0:
        board_parts = [f"{b} -{c}" for b, c in sorted(gb.items(), key=lambda x: -x[1])]
        lines.append(f"    - {', '.join(board_parts)}")

    # 信号表新鲜度
    lines.append("")
    sigs = report.get("signals", {})
    stale_list = [v for v in sigs.values() if v.get("available") and v.get("status") != "ok"]
    fresh_list = [v for v in sigs.values() if v.get("available") and v.get("status") == "ok"]

    if stale_list:
        lines.append("| 信号表 | 最新日期 | 滞后 |")
        lines.append("|--------|---------|------|")
        for v in sorted(stale_list, key=lambda x: x.get("stale_days", 0), reverse=True):
            icon = _ICON.get(v.get("status", "ok"), "❓")
            lines.append(f"| {v.get('label', '?')} | {v.get('last_date', '?')} | {icon} {v.get('stale_days', '?')} 天 |")
    if fresh_list:
        fresh_names = "、".join(v.get("label", "") for v in fresh_list)
        lines.append(f"- 信号正常: {fresh_names}")

    # 日线数据概况
    lines.append("")
    db = report.get("daily_bars", {})
    if db.get("total"):
        board_parts = [f"{b} {c}只" for b, c in db.get("by_board", {}).items() if c > 0]
        lines.append(f"- **日线**: {db['total']} 只股票 ({', '.join(board_parts)}) 截至 {db['latest_date']}")

    return "\n".join(lines) + "\n"


# ── 主函数 ──


def generate_report(date: str) -> str:
    """生成完整日报 Markdown 文本。"""
    from sniper.engine.backtest import BacktestEngine
    from sniper.engine.metrics import calculate_metrics
    from sniper.layers.l0_market import MarketScorer
    from sniper.data_router import DataRouter

    # 检查是否为交易日
    _dr = DataRouter()
    _tdf = _dr.get_trading_dates()
    if not _tdf.empty and "date" in _tdf.columns and date not in set(_tdf["date"].tolist()):
        logger.warning(f"非交易日 {date}，跳过日报生成")
        return ""

    logger.info(f"生成日报: {date}")

    # 跑一天的引擎获取交易/持仓数据
    engine = BacktestEngine()
    result = engine.run(date, date, use_precomputed=True)
    m = calculate_metrics(result.get("daily_values", []),
                          result.get("trades", []), 1_000_000)
    result["m"] = m

    # 采集 L0 市场指纹
    router = DataRouter()
    scorer = MarketScorer(router)
    l0_info = scorer.score_all(date)

    # 采集 L1
    from sniper.layers.l1_sector import SectorScorer
    sector = SectorScorer(router)
    top_sectors = sector.top_sectors(date, top_n=3)

    lines = [
        f"# 📊 量化日报 {date}",
        "",
        f"> 生成时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "---",
        "",
    ]

    lines.append(section_market(date))
    lines.append("---\n")
    lines.append(section_sectors(date))
    lines.append("---\n")

    sec3, candidates, sector_ranks = section_stocks(date, top_sectors)
    lines.append(sec3)
    lines.append("---\n")
    lines.append(section_entry_filter(candidates, date, sector_ranks))
    lines.append("---\n")
    lines.append(section_portfolio(result))
    lines.append("---\n")
    lines.append(section_attribution(date, l0_info))
    lines.append("---\n")
    lines.append(section_config())
    lines.append("---\n")
    lines.append(section_data_source(date))

    # 第九节：传统质量校验（仅在有个股持仓数据时补充）
    try:
        _symbols = []
        stock_df = DataRouter().get_stock_list(status="active")
        if stock_df is not None and not stock_df.empty:
            _symbols = stock_df["symbol"].head(200).tolist()
        if _symbols:
            from data.quality import generate_quality_section
            from shared.fetcher import Fetcher
            fetcher = Fetcher()
            quality_section = generate_quality_section(
                DataRouter().wh, fetcher, _symbols)
            lines.append("---\n")
            lines.append(quality_section)
            lines.append("")
    except Exception as e:
        logger.debug(f"数据质量节生成跳过: {e}")

    return "\n".join(lines)


def main():
    import argparse
    a = argparse.ArgumentParser(description="量化日报生成")
    a.add_argument("--date", type=str, default="", help="日报日期 YYYY-MM-DD，默认当天")
    a.add_argument("--output", type=str, default="", help="输出路径，默认打印到终端")
    args = a.parse_args()

    date = args.date or dt.datetime.now().strftime("%Y-%m-%d")
    report = generate_report(date)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"日报已写入: {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
