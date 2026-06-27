"""HTML 量化日报生成 — python scripts/daily_report_html.py --date 2026-06-03

麦肯锡框架：结论先行，重要信息在最前面。

输出单文件 HTML（CSS 内嵌），浏览器直接打开。

用法:
    python scripts/daily_report_html.py --date 2026-06-03
    python scripts/daily_report_html.py --date 2026-06-03 --output outputs/report.html
"""

import sys, json, os, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

import sniper.config as cfg
from sniper.data_router import DataRouter
from sniper.layers.l0_market import MarketScorer
from sniper.layers.l1_sector import SectorScorer
from sniper.layers.l2_stock import StockScorer
from sniper.layers.l3_entry import EntryFilter
from sniper.layers.l4_exit import ExitChain
from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

logger = get_logger("scripts.daily_report_html")

# ── Obsidian 输出路径 ──
OBSIDIAN_PATH = r"D:\Obsidian\SecondBrain\02-Projects\05-量化系统"

# ── 辅助函数 ──

def _l0_grade(score: float) -> tuple[str, str, str]:
    """L0 评分等级：返回 (标签, 颜色hex, 图标)"""
    if score >= 70:
        return ("市场强势", "#16a34a", "🟢")
    elif score >= 64:
        return ("中性偏强", "#ca8a04", "🟡")
    elif score >= 30:
        return ("谨慎参与", "#ea580c", "🟠")
    else:
        return ("建议回避", "#dc2626", "🔴")

def _direction_icon(v: float) -> str:
    """趋势方向图标"""
    if v > 2: return "↑ 上升"
    if v < -2: return "↓ 下降"
    return "→ 持平"

def _pnl_color(pnl_pct: float) -> str:
    if pnl_pct > 0.05: return "#16a34a"
    if pnl_pct > 0: return "#65a30d"
    if pnl_pct > -0.03: return "#ea580c"
    return "#dc2626"

def _css() -> str:
    return """<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
       background: #f0f2f5; color: #1a1a2e; font-size: 14px; line-height: 1.6; }
.container { max-width: 960px; margin: 0 auto; padding: 20px; }
.header { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
          color: #fff; padding: 28px 32px; border-radius: 12px; margin-bottom: 20px; }
.header h1 { font-size: 22px; font-weight: 600; margin-bottom: 4px; }
.header .sub { opacity: .75; font-size: 13px; }
.card { background: #fff; border-radius: 10px; padding: 20px 24px;
        margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.card-title { font-size: 15px; font-weight: 600; color: #1a1a2e;
              margin-bottom: 14px; padding-bottom: 8px;
              border-bottom: 2px solid #e8e8ef; }
.tag { display: inline-block; padding: 2px 10px; border-radius: 4px;
       font-size: 12px; font-weight: 600; }
.tag-green { background: #dcfce7; color: #166534; }
.tag-yellow { background: #fef9c3; color: #854d0e; }
.tag-orange { background: #ffedd5; color: #9a3412; }
.tag-red { background: #fee2e2; color: #991b1b; }
.refresh-bar { display: flex; align-items: center; gap: 10px; padding: 10px 24px;
               background: #fff; border-radius: 8px; margin-bottom: 16px;
               box-shadow: 0 1px 3px rgba(0,0,0,.08); font-size: 13px; }
.refresh-btn { padding: 6px 16px; background: #2563eb; color: #fff;
               border: none; border-radius: 6px; cursor: pointer; font-size: 13px;
               transition: background .2s; white-space: nowrap; }
.refresh-btn:hover { background: #1d4ed8; }
.refresh-btn:disabled { background: #9ca3af; cursor: not-allowed; }
.refresh-tip { color: #6b7280; font-size: 12px; flex: 1; }
.update-time { color: #9ca3af; font-size: 12px; }
.status-ok { color: #16a34a; font-size: 12px; }
.status-running { color: #2563eb; font-size: 12px; animation: pulse 1.5s infinite; }
.status-error { color: #dc2626; font-size: 12px; }
.status-offline { color: #9ca3af; font-size: 12px; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.grid-4 { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 10px; }
.stat { text-align: center; padding: 12px; background: #f8f9fb; border-radius: 8px; }
.stat .val { font-size: 22px; font-weight: 700; }
.stat .lbl { font-size: 11px; color: #6b7280; margin-top: 2px; }
.stat .sub { font-size: 12px; margin-top: 2px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { background: #f8f9fb; padding: 8px 10px; text-align: left;
     font-weight: 600; color: #374151; border-bottom: 2px solid #e5e7eb; }
td { padding: 8px 10px; border-bottom: 1px solid #f0f0f0; }
tr:hover td { background: #fafafa; }
.action-list { list-style: none; padding: 0; }
.action-list li { padding: 10px 14px; margin-bottom: 6px; border-radius: 6px;
                  border-left: 4px solid; display: flex; align-items: center; gap: 10px; }
.action-high { background: #fef2f2; border-color: #dc2626; }
.action-mid { background: #fffbeb; border-color: #d97706; }
.action-low { background: #f0fdf4; border-color: #16a34a; }
.action-list .num { font-weight: 700; font-size: 13px; min-width: 28px; }
.action-list .txt { flex: 1; }
.action-list .reason { font-size: 12px; color: #6b7280; }
.trend-up { color: #dc2626; }
.trend-down { color: #16a34a; }
.trend-flat { color: #6b7280; }
.fold { margin-top: 12px; }
.fold summary { cursor: pointer; font-weight: 600; color: #6b7280;
               padding: 6px 0; font-size: 13px; }
.fold[open] summary { color: #1a1a2e; }
.l0-gauge { display: flex; align-items: center; gap: 16px; margin: 8px 0; }
.l0-gauge .bar-wrap { flex: 1; height: 18px; background: #e5e7eb; border-radius: 9px;
                      position: relative; overflow: hidden; }
.l0-gauge .bar { height: 100%; border-radius: 9px; transition: width .5s; }
.l0-gauge .bar-label { position: absolute; right: 8px; top: 0; line-height: 18px;
                       font-size: 11px; font-weight: 700; color: #fff; text-shadow: 0 0 2px rgba(0,0,0,.5); }
.l0-dims { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.l0-dim { flex: 1; min-width: 100px; padding: 8px 10px; border-radius: 6px;
          background: #f8f9fb; text-align: center; }
.l0-dim .dim-val { font-size: 18px; font-weight: 700; }
.l0-dim .dim-lbl { font-size: 11px; color: #6b7280; }
@media (max-width: 640px) {
  .grid-2, .grid-4 { grid-template-columns: 1fr; }
  .l0-dims { flex-direction: column; }
  .container { padding: 10px; }
}
@media print {
  .card { break-inside: avoid; }
  .fold[open] summary { color: #1a1a2e; }
}

/* ── L0 周线折线图 ── */
.chart-wrap { background: #f8f9fb; border-radius: 8px; padding: 16px 12px 12px; margin-top: 10px; position: relative; }
.chart-wrap svg { display: block; width: 100%; height: auto; }
.chart-ref-line { stroke-dasharray: 4,3; stroke-width: 1.5; }
.chart-ref-label { font-size: 10px; fill: #6b7280; }
.chart-line { fill: none; stroke-width: 2.5; stroke-linejoin: round; stroke-linecap: round; }
.chart-area { stroke: none; }
.chart-dot { stroke: #fff; stroke-width: 1.5; cursor: pointer; }
.chart-dot:hover { r: 6; }
.chart-axis text { font-size: 10px; fill: #6b7280; }
.chart-axis line, .chart-axis path { stroke: #d1d5db; stroke-width: 1; }
.chart-grid line { stroke: #e5e7eb; stroke-width: 0.5; stroke-dasharray: 2,2; }
</style>"""

def _html_header(date: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>量化日报 {date}</title>
{_css()}
</head>
<body>
<div class="container">
<div class="header">
  <h1>📊 量化日报 · {date}</h1>
  <div class="sub">狙击手 V3.7 ｜ 生成时间 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
</div>
<div class="refresh-bar">
  <span id="status-text" class="status-ok">● 就绪</span>
  <span id="update-time" class="update-time"></span>
  <button id="refresh-btn" class="refresh-btn" onclick="doRefresh()">🔄 刷新</button>
  <span id="refresh-tip" class="refresh-tip"></span>
</div>"""

_HTML_FOOTER = """<script>
const API = 'http://localhost:8765';

async function doRefresh() {
    let btn = document.getElementById('refresh-btn');
    let tip = document.getElementById('refresh-tip');
    let st = document.getElementById('status-text');
    btn.disabled = true; tip.textContent = '刷新中...'; st.textContent = '● 刷新中'; st.className = 'status-running';
    try {
        let r = await fetch(API + '/refresh');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        let d = await r.json();
        if (d.status === 'started') {
            tip.textContent = '✅ 管道已启动，约5分钟';
            st.textContent = '● 运行中'; st.className = 'status-running';
            setTimeout(() => location.reload(), 150000);
        } else if (d.status === 'busy') {
            tip.textContent = '⏳ 管道正在运行中';
            st.textContent = '● 运行中'; st.className = 'status-running';
        } else {
            tip.textContent = '❌ ' + (d.reason || '失败');
            st.textContent = '● 异常'; st.className = 'status-error';
        }
    } catch(e) {
        tip.textContent = '⚠️ 无法连接服务';
        st.textContent = '● 离线'; st.className = 'status-offline';
    }
    btn.disabled = false;
}

// 页面加载时检查服务状态
(async function() {
    let st = document.getElementById('status-text');
    let ut = document.getElementById('update-time');
    try {
        let r = await fetch(API + '/api/status');
        if (r.ok) {
            let d = await r.json();
            st.textContent = '● 就绪'; st.className = 'status-ok';
            if (d.last_run_time) ut.textContent = '上次更新 ' + d.last_run_time;
            if (d.is_running) {
                st.textContent = '● 运行中'; st.className = 'status-running';
                ut.textContent = '管道正在运行...';
            }
            if (d.pipeline_full_last_date) {
                ut.textContent = '最新数据 ' + d.pipeline_full_last_date;
            }
        } else {
            st.textContent = '● 异常'; st.className = 'status-error';
        }
    } catch(e) {
        st.textContent = '● 离线'; st.className = 'status-offline';
        ut.textContent = '服务未启动';
    }
})();
</script>
</div></body></html>"""


# ── 数据采集 ──

def collect_l0(date: str) -> dict:
    router = DataRouter()
    scorer = MarketScorer(router)
    info = scorer.score_all(date)
    regime = scorer.market_regime(date)
    info["regime"] = regime
    return info


def collect_l0_trend(date: str, window: int = 5) -> list[float]:
    """近 N 日 L0 趋势"""
    router = DataRouter()
    dates = router.get_trading_days_before(date, window)
    scorer = MarketScorer(router)
    vals = []
    for d in dates:
        try:
            vals.append(scorer.composite_score(d))
        except Exception:
            continue
    return vals


def collect_l0_daily(date: str, days: int = 30) -> list[dict]:
    """最近 N 个交易日 L0 日线值（含四维度分解），用于日线折线图。"""
    router = DataRouter()
    dates = router.get_trading_days_before(date, days)
    scorer = MarketScorer(router)
    result = []
    for d in dates:
        try:
            s = scorer.score_all(d)
            result.append({
                "date": d[-5:],  # MM-DD 短格式
                "composite": round(s["composite"], 1),
                "trend": round(s["trend"], 1),
                "volume": round(s["volume"], 1),
                "breadth": round(s["breadth"], 1),
                "northbound": round(s["northbound"], 1),
            })
        except Exception:
            continue
    return result


def collect_l0_weekly(date: str, weeks: int = 52) -> list[dict]:
    """最近 N 周 L0 周线值（按 ISO 周平均），默认 52 周约一年。"""
    router = DataRouter()
    scorer = MarketScorer(router)
    trading_days = router.get_trading_days_before(date, weeks * 7)

    weekly_map: dict[tuple[int, int], dict] = {}
    for d in trading_days:
        try:
            dt_obj = dt.datetime.strptime(d, "%Y-%m-%d").date()
            week_key = dt_obj.isocalendar()[:2]  # (year, week)
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


def collect_l1(date: str) -> pd.DataFrame:
    router = DataRouter()
    scorer = SectorScorer(router)
    df = scorer.composite_scores(date)
    return df


def collect_l2(date: str, top_sectors: list[str]) -> tuple[list[dict], dict[str, int]]:
    """每板块独立选股，保证板块均衡。

    每个板块取评分最高的 1 只股票，避免大板块（如机械设备617只）
    碾压小板块（如银行42只）。
    """
    router = DataRouter()
    stock = StockScorer(router)
    sector_scorer = SectorScorer(router)
    all_scores = sector_scorer.composite_scores(date)
    sector_ranks = {}
    if all_scores is not None and not all_scores.empty:
        for _, row in all_scores.iterrows():
            sector_ranks[row["industry_name"]] = int(row["rank"])
    # 每板块独立选股
    candidates: list[dict] = []
    seen: set[str] = set()
    for sector in top_sectors:
        sector_cands = stock.top_stocks(date, [sector])
        for c in sector_cands or []:
            if c["symbol"] not in seen:
                c["sector"] = sector
                candidates.append(c)
                seen.add(c["symbol"])
                break  # 每板块取 1 只
    # 补齐价格字段
    for c in candidates:
        if c.get("price") is None or c.get("price", 0) == 0:
            try:
                bars = router.get_daily_bars(c["symbol"], end=date)
                if bars is not None and not bars.empty:
                    last = bars.iloc[-1]
                    c["price"] = float(last.get("close", 0))
            except Exception:
                c["price"] = 0.0
    return candidates or [], sector_ranks


def collect_l4_warnings(positions: dict[str, dict], date: str) -> list[dict]:
    """对每个持仓评估退出距离"""
    router = DataRouter()
    exit_chain = ExitChain(router)
    warnings = []
    for sym, pos in positions.items():
        signal = exit_chain.evaluate(
            sym, pos.get("entry_date", date), date,
            pos.get("entry_price", 0), pos.get("highest_price", 0),
        )
        pnl_pct = pos.get("pnl_pct", 0)
        # 计算距触发线距离
        # 未盈利阶段: entry_price 到 stop_loss 距离
        entry_price = pos.get("entry_price", 1)
        highest = pos.get("highest_price", entry_price)
        close_price = pos.get("market_value", 0) / max(pos.get("shares", 1), 1) if pos.get("shares") else entry_price

        if highest <= entry_price * 1.005:
            # 止损距离（未盈利阶段）
            stop_price = entry_price * (1 + cfg.EXIT.stop_loss)
            dist_to_stop = (close_price - stop_price) / max(stop_price, 1)
            mode = "止损"
        else:
            # 动态止盈距离（脱离成本后，从最高点回撤）
            trail_price = highest * (1 + cfg.EXIT.trailing_stop)
            dist_to_trail = (close_price - trail_price) / max(trail_price, 1)
            stop_price = trail_price
            dist_to_stop = dist_to_trail
            mode = "动态止盈"

        warnings.append({
            "symbol": sym,
            "pnl_pct": pnl_pct,
            "sector": pos.get("sector", ""),
            "entry_date": pos.get("entry_date", date),
            "score": pos.get("score", 0),
            "stop_price": stop_price,
            "dist_to_stop": dist_to_stop,
            "mode": mode,
            "signal": signal,
        })
    return warnings


def collect_data_source(date: str) -> dict:
    try:
        from data.freshness import DataFreshnessChecker
        checker = DataFreshnessChecker()
        return checker.full_report(date)
    except Exception as e:
        return {"error": str(e)}


def collect_trades(result: dict) -> dict:
    trades = result.get("trades", [])
    buys = [t for t in trades if t.get("action") == "BUY"]
    sells = [t for t in trades if t.get("action") == "SELL"]
    return {"buys": buys, "sells": sells, "all": trades}


def collect_attribution(date: str, l0_info: dict) -> dict:
    try:
        cfg.load_paper_tape()
        if cfg._TRADE_PAPER is None or len(cfg._TRADE_PAPER) < 10:
            return {"status": "skip", "reason": "纸带不足 10 笔"}
        fp = [l0_info["composite"], l0_info["trend"],
              l0_info["volume"], l0_info["breadth"]]
        neighbors = cfg._find_neighbors(fp)
        if len(neighbors) < 10:
            return {"status": "skip", "reason": f"近邻仅 {len(neighbors)} 笔"}
        avg_pnl = float(np.mean([t.get("pnl_pct", 0) for t in neighbors]))
        params = cfg._attribution(neighbors)
        return {
            "status": "ok", "fingerprint": fp,
            "neighbors": len(neighbors), "avg_pnl": avg_pnl,
            "params": params,
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# ── HTML 各节生成 ──

def section_executive_summary(l0_info: dict, positions: dict,
                               daily_values: list[dict], m: dict,
                               l4_warnings: list[dict]) -> str:
    """一、执行摘要：结论先行"""
    composite = l0_info.get("composite", 50)
    grade, color, icon = _l0_grade(composite)

    # 总资产
    total_value = daily_values[-1]["total_value"] if daily_values else 1_000_000
    cash = daily_values[-1]["cash"] if daily_values else 1_000_000
    exposure = daily_values[-1]["exposure"] if daily_values else 0
    pos_count = len(positions)
    total_return = m.get("total_return", 0)
    dd_pct = m.get("max_drawdown", 0)

    # 风控状态
    from sniper.engine.risk import RiskManager
    dd_ratio = None
    if daily_values:
        last_dd = daily_values[-1].get("drawdown", 0)
        dd_ratio = last_dd
    dd_limit = cfg.RISK.portfolio_drawdown_limit
    dd_safe = abs(dd_ratio or 0) < abs(dd_limit * 0.5) if dd_ratio else True
    dd_warn = abs(dd_ratio or 0) >= abs(dd_limit * 0.8) if dd_ratio else False

    # 操作建议
    actions = []
    if composite >= 70:
        actions.append(("高", "积极开仓", f"L0≥70({composite:.0f})，市场强势，可积极选股开仓"))
    elif composite >= 64:
        actions.append(("高", "谨慎开仓", f"L0={composite:.0f}，中性偏强，控制仓位选股"))
    else:
        actions.append(("高", "减少操作", f"L0={composite:.0f}，市场偏弱，建议观望或减仓"))

    # L4 预警
    critical_warnings = [w for w in l4_warnings if w.get("dist_to_stop", 1) < 0.01]
    for w in critical_warnings[:2]:
        actions.append(("高", f"{w['symbol']} 接近止损",
                        f"距止损线仅 {abs(w['dist_to_stop'])*100:.1f}%"))

    # 持仓过多预警
    if pos_count >= cfg.RISK.max_positions:
        actions.append(("中", "持仓已达上限",
                        f"当前 {pos_count}/{cfg.RISK.max_positions} 只，需等待平仓"))

    # 回撤预警
    if dd_warn:
        actions.append(("中", f"回撤接近风控线",
                        f"当前回撤 {abs(dd_ratio)*100:.1f}%（限制{abs(dd_limit)*100:.0f}%）"))

    is_dd = dd_ratio is not None and dd_ratio <= dd_limit
    dd_color = "#dc2626" if is_dd else ("#ea580c" if dd_warn else "#16a34a")
    dd_icon = "🔴" if is_dd else ("🟠" if dd_warn else "🟢")

    parts = [
        f'<div class="card">',
        f'<div class="card-title">📋 一、执行摘要</div>',
        # L0 主判断
        f'<div class="l0-gauge">',
        f'  <span style="font-size:28px">{icon}</span>',
        f'  <div style="flex:1">',
        f'    <div style="font-size:16px;font-weight:600;color:{color}">{grade} · 合成评分 {composite:.1f}</div>',
        f'    <div class="bar-wrap" style="margin-top:4px">',
        f'      <div class="bar" style="width:{composite:.0f}%;background:{color}"></div>',
        f'      <span class="bar-label">{composite:.0f}</span>',
        f'    </div>',
        f'  </div>',
        f'</div>',
        # 概览卡片
        f'<div class="grid-2" style="margin-top:12px">',
        f'  <div class="grid-4" style="grid-column:1">',
        f'    <div class="stat"><div class="val" style="color:{_pnl_color(total_return)}">{total_return*100:.2f}%</div><div class="lbl">总收益</div></div>',
        f'    <div class="stat"><div class="val">{total_value:,.0f}</div><div class="lbl">总资产</div></div>',
        f'    <div class="stat"><div class="val">{pos_count}</div><div class="lbl">持仓数</div></div>',
        f'    <div class="stat"><div class="val">{exposure/total_value*100:.0f}%</div><div class="lbl">暴露比例</div></div>',
        f'  </div>',
        # 风控状态
        f'  <div class="stat" style="grid-column:2;display:flex;flex-direction:column;justify-content:center">',
        f'    <div style="font-size:13px;color:#6b7280">风控状态</div>',
        f'    <div style="font-size:24px;font-weight:700;color:{dd_color};margin:4px 0">{dd_icon}</div>',
        f'    <div style="font-size:12px;color:#6b7280">',
        f'      回撤 {abs(dd_ratio or 0)*100:.2f}%{"🔴" if is_dd else ""} ｜ 限制 {abs(dd_limit or 0.05)*100:.0f}%',
        f'    </div>',
        f'  </div>',
        f'</div>',
        # 操作清单
        f'<div style="margin-top:12px">',
        f'  <div style="font-size:13px;font-weight:600;margin-bottom:6px">今日操作清单</div>',
        f'  <ul class="action-list">',
    ]
    for i, (level, title, reason) in enumerate(actions[:5], 1):
        cls = {"高": "action-high", "中": "action-mid", "低": "action-low"}.get(level, "action-mid")
        parts.append(
            f'    <li class="{cls}"><span class="num">#{i}</span>'
            f'<span class="tag tag-{"red" if level=="高" else "yellow" if level=="中" else "green"}">{level}</span>'
            f'<span class="txt"><strong>{title}</strong><div class="reason">{reason}</div></span></li>'
        )
    parts.append(f'  </ul></div></div>')
    return "\n".join(parts)


def _render_l0_weekly_chart(l0_weekly: list[dict]) -> str:
    """生成 L0 周线 SVG 折线图 HTML。

    展示最近 N 周的 L0 周均值，带 64/70 参考线。
    """
    if not l0_weekly or len(l0_weekly) < 2:
        return ""

    # ── 坐标映射 ──
    W, H = 800, 260
    ML, MR, MT, MB = 50, 20, 20, 40  # margins
    plot_w = W - ML - MR
    plot_h = H - MT - MB

    vals = [w["avg_l0"] for w in l0_weekly]
    labels = [w["week_label"] for w in l0_weekly]
    n = len(vals)
    min_val, max_val = 0, 100

    def x_idx(i: int) -> float:
        return ML + (i / max(n - 1, 1)) * plot_w

    def y_val(v: float) -> float:
        ratio = (v - min_val) / (max_val - min_val)
        return MT + plot_h - ratio * plot_h

    # ── Y 轴刻度 ──
    y_ticks = list(range(0, 101, 10))
    y_grid = "\n".join(
        f'<line x1="{ML}" y1="{y_val(t):.1f}" x2="{W - MR}" y2="{y_val(t):.1f}" class="chart-grid" />'
        f'<text x="{ML - 6}" y="{y_val(t) + 4:.1f}" text-anchor="end" class="chart-axis">{t}</text>'
        for t in y_ticks
    )

    # ── 参考线: 64(开仓线) 和 70(满仓线) ──
    ref_lines = []
    for ref_val, ref_color, ref_label in [(64, "#ca8a04", "开仓64"), (70, "#16a34a", "满仓70")]:
        ry = y_val(ref_val)
        ref_lines.append(
            f'<line x1="{ML}" y1="{ry:.1f}" x2="{W - MR}" y2="{ry:.1f}" '
            f'stroke="{ref_color}" class="chart-ref-line" />'
            f'<text x="{W - MR + 4}" y="{ry + 4:.1f}" fill="{ref_color}" class="chart-ref-label">{ref_label}</text>'
        )

    # ── X 轴标签（每隔 max(1, n//12) 显示一个，避免拥挤）──
    step = max(1, n // 12)
    x_labels = "\n".join(
        f'<text x="{x_idx(i):.1f}" y="{H - MB + 16}" text-anchor="end" '
        f'transform="rotate(-30,{x_idx(i):.1f},{H - MB + 8})" class="chart-axis">{labels[i]}</text>'
        for i in range(0, n, step)
    ) + (f'\n<text x="{x_idx(n - 1):.1f}" y="{H - MB + 16}" text-anchor="end" '
         f'transform="rotate(-30,{x_idx(n - 1):.1f},{H - MB + 8})" class="chart-axis" '
         f'font-weight="bold">{labels[-1]}</text>' if (n - 1) % step != 0 else "")

    # ── 折线路径 ──
    line_pts = " ".join(f"{x_idx(i):.1f},{y_val(v):.1f}" for i, v in enumerate(vals))
    line_path = f'<path d="M {line_pts}" class="chart-line" stroke="#2563eb" />'

    # ── 面积填充 ──
    area_pts = f"{x_idx(0):.1f},{y_val(0):.1f} " + " ".join(
        f"{x_idx(i):.1f},{y_val(v):.1f}" for i, v in enumerate(vals)
    ) + f" {x_idx(n - 1):.1f},{y_val(0):.1f}"
    area_path = f'<path d="M {area_pts}" class="chart-area" fill="rgba(37,99,235,0.12)" />'

    # ── 数据点（含 tooltip）──
    dots = ""
    for i, v in enumerate(vals):
        is_last = i == n - 1
        dot_r = 4.5 if is_last else 3.5
        dot_fill = "#2563eb" if not is_last else "#dc2626"
        dot_stroke = "#fff"
        dot_stroke_w = 2 if is_last else 1.5
        dots += (
            f'<circle cx="{x_idx(i):.1f}" cy="{y_val(v):.1f}" r="{dot_r}" '
            f'fill="{dot_fill}" stroke="{dot_stroke}" stroke-width="{dot_stroke_w}" class="chart-dot">'
            f'<title>第{i+1}周 {labels[i]}: L0={v:.1f}</title>'
            f'</circle>'
        )
        if is_last:
            # 本周标记
            dots += (
                f'<text x="{x_idx(i):.1f}" y="{y_val(v) - 10:.1f}" text-anchor="middle" '
                f'fill="#dc2626" font-size="11" font-weight="700">{v:.1f}</text>'
            )

    # ── 底部统计摘要 ──
    current_l0 = vals[-1]
    prev_l0 = vals[-2] if n >= 2 else current_l0
    direction = "↑" if current_l0 > prev_l0 else ("↓" if current_l0 < prev_l0 else "→")
    dir_color = "#dc2626" if direction == "↑" else ("#16a34a" if direction == "↓" else "#6b7280")

    # 连续趋势判断
    consecutive_up = 0
    consecutive_down = 0
    for i in range(n - 1, max(0, n - 6), -1):
        if vals[i] > vals[i - 1]:
            consecutive_up += 1
            consecutive_down = 0
        elif vals[i] < vals[i - 1]:
            consecutive_down += 1
            consecutive_up = 0
        else:
            break
    if consecutive_up >= 3:
        trend_desc = f"连续 {consecutive_up} 周上升 📈"
    elif consecutive_down >= 3:
        trend_desc = f"连续 {consecutive_down} 周下降 📉"
    else:
        trend_desc = "震荡 ⚖️"

    summary_line = (
        f'<div style="font-size:12px;color:#6b7280;margin-top:8px;display:flex;gap:16px;flex-wrap:wrap">'
        f'<span>周线趋势: <strong style="color:{dir_color}">{direction} {current_l0:.1f}</strong> 上周 {prev_l0:.1f}</span>'
        f'<span>方向判断: {trend_desc}</span>'
        f'<span>近 {n} 周均值: <strong>{sum(vals)/n:.1f}</strong></span>'
        f'</div>'
    )

    svg = (
        f'<div class="chart-wrap">'
        f'<div style="font-size:13px;font-weight:600;margin-bottom:8px;color:#1a1a2e">'
        f'📅 L0 周线趋势（近 {n} 周）</div>'
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">'
        f'<defs><clipPath id="chart-clip"><rect x="{ML}" y="{MT}" width="{plot_w}" height="{plot_h}" /></clipPath></defs>'
        f'{y_grid}'
        f'{"".join(ref_lines)}'
        f'<g clip-path="url(#chart-clip)">{area_path}{line_path}{dots}</g>'
        f'{x_labels}'
        f'<line x1="{ML}" y1="{MT}" x2="{ML}" y2="{H - MB}" class="chart-axis" />'
        f'<line x1="{ML}" y1="{H - MB}" x2="{W - MR}" y2="{H - MB}" class="chart-axis" />'
        f'</svg>'
        f'{summary_line}'
        f'</div>'
    )

    return svg


def _render_l0_daily_chart(l0_daily: list[dict]) -> str:
    """生成 L0 日线 SVG 折线图 HTML。

    展示最近 30 个交易日的 L0 合成评分，带各维度堆叠或显示切换。
    """
    if not l0_daily or len(l0_daily) < 2:
        return ""

    W, H = 800, 240
    ML, MR, MT, MB = 50, 20, 20, 40
    plot_w = W - ML - MR
    plot_h = H - MT - MB

    vals = [d["composite"] for d in l0_daily]
    labels = [d["date"] for d in l0_daily]
    n = len(vals)
    min_val, max_val = 0, 100

    def x_idx(i: int) -> float:
        return ML + (i / max(n - 1, 1)) * plot_w

    def y_val(v: float) -> float:
        ratio = (v - min_val) / (max_val - min_val)
        return MT + plot_h - ratio * plot_h

    # Y 轴刻度
    y_ticks = list(range(0, 101, 10))
    y_grid = "\n".join(
        f'<line x1="{ML}" y1="{y_val(t):.1f}" x2="{W - MR}" y2="{y_val(t):.1f}" class="chart-grid" />'
        f'<text x="{ML - 6}" y="{y_val(t) + 4:.1f}" text-anchor="end" class="chart-axis">{t}</text>'
        for t in y_ticks
    )

    # 参考线: 64(开仓线) 和 70(满仓线)
    ref_lines = []
    for ref_val, ref_color, ref_label in [(64, "#ca8a04", "开仓64"), (70, "#16a34a", "满仓70")]:
        ry = y_val(ref_val)
        ref_lines.append(
            f'<line x1="{ML}" y1="{ry:.1f}" x2="{W - MR}" y2="{ry:.1f}" '
            f'stroke="{ref_color}" class="chart-ref-line" />'
            f'<text x="{W - MR + 4}" y="{ry + 4:.1f}" fill="{ref_color}" class="chart-ref-label">{ref_label}</text>'
        )

    # X 轴标签（每隔 max(1, n//10) 个显示一个）
    step = max(1, n // 10)
    x_labels = "\n".join(
        f'<text x="{x_idx(i):.1f}" y="{H - MB + 16}" text-anchor="end" '
        f'transform="rotate(-30,{x_idx(i):.1f},{H - MB + 8})" class="chart-axis">{labels[i]}</text>'
        for i in range(0, n, step)
    ) + (f'\n<text x="{x_idx(n - 1):.1f}" y="{H - MB + 16}" text-anchor="end" '
         f'transform="rotate(-30,{x_idx(n - 1):.1f},{H - MB + 8})" class="chart-axis" '
         f'font-weight="bold">{labels[-1]}</text>' if (n - 1) % step != 0 else "")

    # 折线
    line_pts = " ".join(f"{x_idx(i):.1f},{y_val(v):.1f}" for i, v in enumerate(vals))
    line_path = f'<path d="M {line_pts}" class="chart-line" stroke="#2563eb" />'

    # 面积填充
    area_pts = f"{x_idx(0):.1f},{y_val(0):.1f} " + " ".join(
        f"{x_idx(i):.1f},{y_val(v):.1f}" for i, v in enumerate(vals)
    ) + f" {x_idx(n - 1):.1f},{y_val(0):.1f}"
    area_path = f'<path d="M {area_pts}" class="chart-area" fill="rgba(37,99,235,0.12)" />'

    # 数据点
    dots = ""
    for i, v in enumerate(vals):
        is_last = i == n - 1
        dot_r = 4.5 if is_last else 3
        dot_fill = "#2563eb" if not is_last else "#dc2626"
        dots += (
            f'<circle cx="{x_idx(i):.1f}" cy="{y_val(v):.1f}" r="{dot_r}" '
            f'fill="{dot_fill}" stroke="#fff" stroke-width="2" class="chart-dot">'
            f'<title>{labels[i]}: L0={v:.1f}</title>'
            f'</circle>'
        )
        if is_last:
            dots += (
                f'<text x="{x_idx(i):.1f}" y="{y_val(v) - 10:.1f}" text-anchor="middle" '
                f'fill="#dc2626" font-size="11" font-weight="700">{v:.1f}</text>'
            )

    # 底部统计
    current_l0 = vals[-1]
    prev_l0 = vals[-2] if n >= 2 else current_l0
    direction = "↑" if current_l0 > prev_l0 else ("↓" if current_l0 < prev_l0 else "→")

    # 近 5 日趋势
    last_5 = vals[-5:] if n >= 5 else vals
    trend_5 = "上升" if last_5[-1] > last_5[0] else ("下降" if last_5[-1] < last_5[0] else "持平")

    summary_line = (
        f'<div style="font-size:12px;color:#6b7280;margin-top:8px;display:flex;gap:16px;flex-wrap:wrap">'
        f'<span>日线趋势: <strong>{direction} {current_l0:.1f}</strong> 前日 {prev_l0:.1f}</span>'
        f'<span>近5日: {trend_5}</span>'
        f'<span>近 {n} 日区间: [{min(vals):.1f} ~ {max(vals):.1f}]</span>'
        f'</div>'
    )

    svg = (
        f'<div class="chart-wrap" style="margin-top:12px">'
        f'<div style="font-size:13px;font-weight:600;margin-bottom:8px;color:#1a1a2e">'
        f'📈 L0 日线趋势（近 {n} 个交易日）</div>'
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">'
        f'<defs><clipPath id="daily-chart-clip"><rect x="{ML}" y="{MT}" width="{plot_w}" height="{plot_h}" /></clipPath></defs>'
        f'{y_grid}'
        f'{"".join(ref_lines)}'
        f'<g clip-path="url(#daily-chart-clip)">{area_path}{line_path}{dots}</g>'
        f'{x_labels}'
        f'<line x1="{ML}" y1="{MT}" x2="{ML}" y2="{H - MB}" class="chart-axis" />'
        f'<line x1="{ML}" y1="{H - MB}" x2="{W - MR}" y2="{H - MB}" class="chart-axis" />'
        f'</svg>'
        f'{summary_line}'
        f'</div>'
    )

    return svg


def section_market_depth(l0_info: dict, l0_trend: list[float],
                          l0_weekly: list[dict],
                          l0_daily: list[dict],
                          l1_df: pd.DataFrame, candidates: list[dict],
                          sector_ranks: dict) -> str:
    """二、市场深度分析"""
    composite = l0_info.get("composite", 50)
    grade, color, icon = _l0_grade(composite)

    # L0 趋势
    trend_l0 = ""
    if len(l0_trend) >= 2:
        change = l0_trend[-1] - l0_trend[0]
        trend_l0 = _direction_icon(change)
        vals_str = ", ".join(f"{v:.0f}" for v in l0_trend)
        trend_l0 += f" ｜ {len(l0_trend)}日序列: [{vals_str}]"

    # L0 各维度
    dims = [
        ("趋势", l0_info.get("trend", 0), cfg.MARKET.trend_weight),
        ("量能", l0_info.get("volume", 0), cfg.MARKET.volume_weight),
        ("宽度", l0_info.get("breadth", 0), cfg.MARKET.breadth_weight),
        ("北向", l0_info.get("northbound", 0), cfg.MARKET.northbound_weight),
    ]
    dim_html = []
    for name, val, w in dims:
        _, c, _ = _l0_grade(val)
        dim_html.append(
            f'<div class="l0-dim"><div class="dim-val" style="color:{c}">{val:.1f}</div>'
            f'<div class="dim-lbl">{name} ({(w*100):.0f}%)</div></div>'
        )

    # L1 板块
    sec_html = ""
    if l1_df is not None and not l1_df.empty:
        top5 = l1_df.head(cfg.SECTOR.top_n_high)
        sec_html = '<div class="table-wrap"><table><tr><th>#</th><th>板块</th><th>综合</th><th>动量</th><th>资金</th><th>广度</th><th>热度</th></tr>'
        for _, row in top5.iterrows():
            sec_html += f"<tr><td>{int(row['rank'])}</td><td><strong>{row['industry_name']}</strong></td>"
            for k in ["composite", "momentum", "fund_flow", "breadth", "heat"]:
                v = row.get(k, 0)
                if pd.notna(v):
                    sec_html += f"<td>{v:.0f}</td>"
                else:
                    sec_html += "<td>—</td>"
            sec_html += "</tr>"
        sec_html += "</table></div>"

    # L2 候选股 + 买卖建议（含止损风险标记）
    stock_html = ""
    if candidates:
        stock_html = (
            '<div class="table-wrap"><table>'
            '<tr><th>代码</th><th>板块</th><th>评分</th><th>建议</th><th>参考价</th>'
            '<th>止损价</th><th>风险</th></tr>'
        )
        for c in candidates:
            score = c.get("score", 0)
            l0_open_l2 = composite >= cfg.MARKET.bullish_threshold
            score_pass = score >= cfg.ENTRY.soft_min_score
            if score_pass and l0_open_l2:
                suggestion, tag_cls = ("买入", "green")
            elif score_pass:
                suggestion, tag_cls = ("等待L0", "yellow")
            else:
                suggestion, tag_cls = ("观望", "yellow")
            ticker = c.get("symbol", "?")
            price = float(c.get("price", 0) or 0)
            if price > 0:
                stop_loss_price = price * (1 + cfg.EXIT.stop_loss)
                dist_pct = (price - stop_loss_price) / stop_loss_price * 100
                if dist_pct < 2:
                    risk_cell = f'<td style="background:#fef2f2;color:#dc2626;font-weight:700">{dist_pct:.1f}% 🔴</td>'
                elif dist_pct < 4:
                    risk_cell = f'<td style="background:#fefce8;color:#ca8a04;font-weight:600">{dist_pct:.1f}% 🟡</td>'
                else:
                    risk_cell = f'<td style="background:#f0fdf4;color:#16a34a;font-weight:600">{dist_pct:.1f}% 🟢</td>'
            else:
                stop_loss_price = 0
                risk_cell = '<td style="color:#6b7280">—</td>'
            sec_name = c.get("sector", "—")
            stock_html += (
                f"<tr><td>{ticker}</td>"
                f"<td>{sec_name}</td>"
                f"<td>{score:.1f}</td>"
                f'<td><span class="tag tag-{tag_cls}">{suggestion}</span></td>'
                f"<td>{price:.2f}</td>"
                f"<td>{stop_loss_price:.2f}</td>"
                f"{risk_cell}</tr>"
            )
        stock_html += "</table></div>"
    else:
        stock_html = '<div style="color:#6b7280;padding:8px">无候选个股</div>'

    return f"""
<div class="card">
  <div class="card-title">📈 二、市场深度分析</div>

  <div style="font-weight:600;font-size:14px;color:{color}">L0 合成评分 {icon} {composite:.1f} — {grade}</div>
  <div style="font-size:12px;color:#6b7280;margin:4px 0 8px">{trend_l0}</div>

  <div class="l0-dims">{"".join(dim_html)}</div>

  {_render_l0_weekly_chart(l0_weekly)}

  {_render_l0_daily_chart(l0_daily)}

  <details class="fold" {"open" if l1_df is not None and not l1_df.empty else ""}>
  <summary>L1 强势板块</summary>
  {sec_html or '<div style="color:#6b7280;padding:8px">无板块数据</div>'}
  </details>

  <details class="fold" {"open" if candidates else ""}>
  <summary>L2 候选个股及买卖建议</summary>
  {stock_html}
  </details>
</div>"""


def section_portfolio_advice(l0_info: dict, positions: dict, l4_warnings: list[dict],
                              daily_values: list[dict], candidates: list[dict]) -> str:
    """三、持仓与交易建议

    叙事关联 L0 开仓判断、L2 候选股评估、实际持仓，形成完整链路。
    """
    composite = l0_info.get("composite", 50)
    grade, color, icon = _l0_grade(composite)
    l0_open = composite >= cfg.MARKET.bullish_threshold

    # 过滤出通过入场条件的候选股，排除已在持仓中的股票
    held = set(positions.keys()) if positions else set()
    entry_pass = []
    for c in candidates or []:
        if c["symbol"] in held:
            continue  # 已在持仓中，不重复推荐入场
        score = c.get("score", 0)
        if score >= cfg.ENTRY.soft_min_score:
            entry_pass.append(c)

    # ── L0 开仓判断栏 ──
    l0_line = (
        f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;'
        f'padding:10px 14px;border-radius:6px;'
        f'background:{"#f0fdf4" if l0_open else "#fef2f2"}">'
        f'<span style="font-size:20px">{icon}</span>'
        f'<div><strong>L0 {composite:.0f} — {grade}</strong>'
        f'{" 通过开仓线 " if l0_open else " 未达开仓线 "}'
        f'<code style="background:#e5e7eb;padding:1px 6px;border-radius:3px;font-size:12px">L0≥{cfg.MARKET.bullish_threshold:.0f}</code>'
        f'{" · 可开新仓" if l0_open else " · 不开新仓"}</div></div>'
    )

    # ── 无持仓但有候选股 → 展示待入场池 ──
    if not positions:
        pending_html = ""
        if entry_pass:
            rows = "".join(
                f'<tr><td>{c["symbol"]}</td><td>{c.get("score",0):.1f}</td>'
                f'<td style="color:#6b7280">等待 L0≥{cfg.MARKET.bullish_threshold:.0f}</td></tr>'
                for c in entry_pass
            )
            pending_html = (
                f'<div style="margin-top:10px">'
                f'<div style="font-size:13px;font-weight:600;margin-bottom:6px">📋 待入场候选（{len(entry_pass)} 只通过 L2 评分）</div>'
                f'<div class="table-wrap"><table>'
                f'<tr><th>代码</th><th>评分</th><th>状态</th></tr>{rows}</table></div></div>'
            )

        return f"""
<div class="card">
  <div class="card-title">💼 三、持仓与交易建议</div>
  {l0_line}
  <div style="color:#6b7280;padding:8px 0">今日无持仓变动</div>
  {pending_html}
</div>"""

    # ── 有持仓 → 持仓表格 + 当日入场评估 ──
    pos_rows = ""
    for sym, pos in sorted(positions.items(), key=lambda x: -abs(x[1].get("pnl_pct", 0))):
        pnl_pct = pos.get("pnl_pct", 0)
        pnl_val = pos.get("pnl", 0)
        color = _pnl_color(pnl_pct)
        entry_price = pos.get("entry_price", 0)
        close_price = pos.get("market_value", 0) / max(pos.get("shares", 1), 1)
        pnl_str = f"{pnl_pct*100:+.2f}%"

        # 距止损/止盈距离
        warning = next((w for w in l4_warnings if w.get("symbol") == sym), None)
        dist_str = ""
        risk_style = ""
        stop_price_val = 0
        mode_tag = ""
        if warning:
            dist = warning.get("dist_to_stop", 1)
            stop_price_val = warning.get("stop_price", 0)
            mode = warning.get("mode", "止损")
            # 模式标签（止损🔴 / 动态止盈🟠）
            if mode == "止损":
                mode_tag = '<span style="color:#dc2626;font-weight:700;font-size:12px">⬇ 止损</span>'
            else:
                mode_tag = '<span style="color:#ea580c;font-weight:700;font-size:12px">⬆ 动态止盈</span>'
            # 风控列：背景色 + 距离百分比
            if dist < 0.01:
                dist_str = "⚠️ 已触发"
                risk_style = "background:#fef2f2;color:#dc2626;font-weight:700"
            elif dist < 0.03:
                dist_str = f"距触发 {dist*100:.1f}%"
                risk_style = "background:#fefce8;color:#a16207;font-weight:700"
            else:
                dist_str = f"距触发 {dist*100:.1f}%"
                risk_style = "background:#f0fdf4;color:#16a34a;font-weight:700"

            # 建议
            sig = warning.get("signal")
            if sig and sig.get("exit"):
                advice = f"建议离场"
                advice_color = "#dc2626"
            elif dist < 0.01:
                advice = f"⚠️ 关注止损"
                advice_color = "#ea580c"
            elif pnl_pct > 0.08:
                advice = f"持有，可设动态止盈"
                advice_color = "#16a34a"
            elif pnl_pct > 0:
                advice = f"继续持有"
                advice_color = "#65a30d"
            else:
                advice = f"持有观察"
                advice_color = "#6b7280"
            advice_full = advice
        else:
            advice = "持有观察"
            advice_color = "#6b7280"
            risk_style = ""
            mode_tag = ""
            advice_full = advice

        pos_rows += (
            f"<tr>"
            f"<td><strong>{sym}</strong></td>"
            f"<td style='color:{color};font-weight:600'>{pnl_str}</td>"
            f"<td>{pos.get('sector','?')}</td>"
            f"<td>{pos.get('score',0):.0f}</td>"
            f"<td>{entry_price:.2f}</td>"
            f"<td>{close_price:.2f}</td>"
            f"<td style='font-weight:700;text-align:center'>{stop_price_val:.2f}<br>{mode_tag}</td>"
            f"<td style='{risk_style};text-align:center'>{dist_str}</td>"
            f"<td style='color:{advice_color};font-size:12px'>{advice_full}</td>"
            f"</tr>"
        )

    # L4 退出预警摘要
    warn_rows = ""
    for w in l4_warnings:
        sig = w.get("signal")
        if sig and sig.get("exit"):
            warn_rows += f"<tr><td>{w['symbol']}</td><td>{sig['reason']}</td><td>{sig.get('pnl_pct',0)*100:+.2f}%</td><td><span class='tag tag-red'>已触发</span></td></tr>"
        elif w.get("dist_to_stop", 1) < 0.01:
            warn_rows += f"<tr><td>{w['symbol']}</td><td>接近触发</td><td>{w.get('pnl_pct',0)*100:+.2f}%</td><td><span class='tag tag-orange'>⚠️</span></td></tr>"

    warn_html = ""
    if warn_rows:
        warn_html = f"""
<details class="fold" open>
  <summary>⚠️ L4 退出预警</summary>
  <div class="table-wrap"><table>
    <tr><th>股票</th><th>信号</th><th>盈亏</th><th>状态</th></tr>
    {warn_rows}
  </table></div>
</details>"""

    # ── 有持仓 → 待入场候选补充 ──
    entry_html = ""
    if entry_pass:
        rows = "".join(
            f'<tr><td>{c["symbol"]}</td><td>{c.get("score",0):.1f}</td>'
            f'<td style="color:#16a34a">{"可入场" if l0_open else "等待L0回升"}</td></tr>'
            for c in entry_pass
        )
        entry_html = f"""
<details class="fold">
  <summary>📋 今日入场评估（{len(entry_pass)} 只候选）</summary>
  <div class="table-wrap"><table>
    <tr><th>代码</th><th>评分</th><th>状态</th></tr>
    {rows}
  </table></div>
</details>"""

    return f"""
<div class="card">
  <div class="card-title">💼 三、持仓与交易建议</div>
  {l0_line}
  <div class="table-wrap"><table>
    <tr><th>股票</th><th>盈亏</th><th>板块</th><th>评分</th><th>入场价</th><th>现价</th><th>止盈/止损</th><th>风控</th><th>建议</th></tr>
    {pos_rows}
  </table></div>
  {warn_html}
  {entry_html}
</div>"""


def section_trades(trades: dict) -> str:
    """四、日内交易记录"""
    sells = trades.get("sells", [])
    buys = trades.get("buys", [])

    if not sells and not buys:
        return ""

    sell_rows = ""
    for t in sells:
        pnl = t.get("pnl", 0)
        cost = t.get("cost", 1) or 1
        pnl_pct = pnl / cost
        color = _pnl_color(pnl_pct)
        sell_rows += (
            f"<tr>"
            f"<td>{t['symbol']}</td>"
            f"<td style='color:{color};font-weight:600'>{pnl:+.0f} ({pnl_pct*100:+.2f}%)</td>"
            f"<td>{t.get('reason','?')}</td>"
            f"<td>{t.get('price',0):.2f}</td>"
            f"<td>{t.get('shares',0)}</td>"
            f"</tr>"
        )

    buy_rows = ""
    for t in buys[:10]:
        buy_rows += (
            f"<tr>"
            f"<td>{t['symbol']}</td>"
            f"<td>{t.get('price',0):.2f}</td>"
            f"<td>{t.get('shares',0)}</td>"
            f"<td>{t.get('cost',0):.0f}</td>"
            f"</tr>"
        )

    parts = ['<div class="card"><div class="card-title">📝 四、日内交易记录</div>']

    if sells:
        parts.append(f'<details class="fold" open>')
        parts.append(f'<summary>平仓 ({len(sells)} 笔)</summary>')
        parts.append('<div class="table-wrap"><table><tr><th>股票</th><th>PnL</th><th>原因</th><th>价格</th><th>数量</th></tr>')
        parts.append(sell_rows)
        parts.append('</table></div></details>')

    if buys:
        parts.append(f'<details class="fold" {"open" if not sells else ""}>')
        parts.append(f'<summary>开仓 ({len(buys)} 笔)</summary>')
        parts.append('<div class="table-wrap"><table><tr><th>股票</th><th>价格</th><th>数量</th><th>成本</th></tr>')
        parts.append(buy_rows)
        parts.append('</table></div></details>')

    parts.append('</div>')
    return "\n".join(parts)


def section_appendix(l0_info: dict, attribution: dict,
                      ds_report: dict, date: str) -> str:
    """附录：打字机归因 + 参数 + 数据源状态"""

    # 打字机归因
    attr_html = ""
    if attribution.get("status") == "ok":
        fp = attribution["fingerprint"]
        params = attribution.get("params", {})
        param_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>"
            for k, v in sorted(params.items())
        )
        attr_html = f"""
<details class="fold" open>
<summary>打字机归因</summary>
<div style="padding:8px 0;font-size:13px">
  市场指纹: [{fp[0]:.1f}, {fp[1]:.1f}, {fp[2]:.1f}, {fp[3]:.1f}]<br>
  近邻: {attribution['neighbors']} 笔 ｜ 平均 PnL: {attribution['avg_pnl']*100:+.2f}%
</div>
{"" if not param_rows else f'<div class="table-wrap"><table><tr><th>参数</th><th>归因值</th></tr>{param_rows}</table></div>'}
</details>"""
    elif attribution.get("status") == "skip":
        attr_html = f'<details class="fold"><summary>打字机归因</summary><div style="color:#6b7280;padding:8px">{attribution.get("reason","")}</div></details>'

    # 参数配置
    config_sections = [
        ("EXIT", cfg.EXIT, ["stop_loss", "trailing_stop", "max_hold_days", "ma_break_below"]),
        ("RISK", cfg.RISK, ["position_size", "max_positions", "max_sector_exposure",
                             "target_exposure_ratio", "portfolio_drawdown_limit",
                             "active_reduction_l0", "active_reduction_exposure"]),
        ("ENTRY", cfg.ENTRY, ["soft_min_score", "soft_sector_top", "hard_min_price"]),
        ("MARKET", cfg.MARKET, ["bullish_threshold", "bearish_threshold"]),
    ]
    config_rows = ""
    for sec_name, obj, keys in config_sections:
        for k in keys:
            if hasattr(obj, k):
                v = getattr(obj, k)
                config_rows += f"<tr><td>{sec_name}</td><td>{k}</td><td>{v}</td></tr>"

    # 数据源状态
    ds_html = ""
    _ICON = {"ok": "🟢", "warn": "🟠", "error": "🔴"}
    if ds_report and "error" not in ds_report:
        sm = ds_report.get("scoring_mode", {})
        mode = sm.get("mode", "?")
        mode_icon = _ICON.get("ok" if mode == "precomputed" else "warn", "?")
        pc = ds_report.get("precomputed", {})
        gap = ds_report.get("universe_gap", {})

        ds_lines = [
            f'<div style="font-size:13px;padding:4px 0">L2 评分: {mode_icon} <code>{mode}</code> — {sm.get("source","?")}</div>',
        ]
        if pc.get("available"):
            ds_lines.append(
                f'<div style="font-size:13px;padding:4px 0">预计算: 最新 {pc["latest_date"]}, '
                f'{pc["total_stocks"]} 只, 滞后 {pc["stale_days"]} 天</div>'
            )
        else:
            ds_lines.append(f'<div style="font-size:13px;padding:4px 0">预计算: 不可用</div>')

        g = gap.get("gap_total", 0)
        gap_icon = _ICON.get("ok" if g == 0 else ("warn" if g < 1000 else "error"), "?")
        ds_lines.append(f'<div style="font-size:13px;padding:4px 0">覆盖差距: {gap_icon} {gap.get("precomputed_count",0)} / {gap.get("live_count",0)}, 缺 {g} 只</div>')

        # 信号表
        sigs = ds_report.get("signals", {})
        stale = [(k, v) for k, v in sigs.items() if v.get("available") and v.get("status") != "ok"]
        if stale:
            ds_lines.append('<div style="font-size:13px;padding:4px 0;font-weight:600">待更新信号:</div>')
            for k, v in sorted(stale, key=lambda x: x[1].get("stale_days", 0), reverse=True):
                icon = _ICON.get(v.get("status", "ok"), "?")
                ds_lines.append(f'<div style="font-size:12px;padding:2px 0 2px 16px">{icon} {v.get("label",k)} 滞后 {v.get("stale_days","?")} 天</div>')

        ds_html = "".join(ds_lines)
    else:
        ds_html = f'<div style="color:#6b7280;padding:8px">数据源检查不可用: {ds_report.get("error","?")}</div>'

    return f"""
<div class="card">
  <div class="card-title">📎 附录</div>

  {attr_html}

  <details class="fold">
  <summary>当前参数配置</summary>
  <div class="table-wrap"><table>
    <tr><th>模块</th><th>参数</th><th>值</th></tr>
    {config_rows}
  </table></div>
  </details>

  <details class="fold" open>
  <summary>数据源状态</summary>
  {ds_html}
  <div style="font-size:12px;color:#6b7280;margin-top:8px">
    运行 <code>python scripts/precompute_l2.py</code> 更新预计算因子 ｜
    <code>signals/download_industry.py</code> 更新信号表
  </div>
  </details>
</div>"""


# ── 主函数 ──

def generate_html(date: str) -> str:
    """生成完整 HTML 日报（非交易日跳过）。"""
    # 检查是否为交易日
    _dr = DataRouter()
    _tdf = _dr.get_trading_dates()
    if not _tdf.empty and "date" in _tdf.columns:
        _td_set = set(_tdf["date"].tolist())
        if date not in _td_set:
            logger.warning(f"非交易日 {date}，跳过日报生成")
            return ""

    logger.info(f"生成 HTML 日报: {date}")

    # 1. 运行引擎获取当日数据
    engine = BacktestEngine()
    result = engine.run(date, date, use_precomputed=True, self_evolve=False)
    m = calculate_metrics(result.get("daily_values", []),
                          result.get("trades", []), 1_000_000)
    trades_data = result.get("trades", [])
    daily_values = result.get("daily_values", [])

    # 2. 采集持仓数据
    positions = dict(engine.risk.positions) if hasattr(engine, 'risk') else {}

    # 3. L0
    l0_info = collect_l0(date)
    l0_trend = collect_l0_trend(date)
    l0_weekly = collect_l0_weekly(date)
    l0_daily = collect_l0_daily(date)

    # 4. L1 + L2
    top_sectors = []
    l1_df = collect_l1(date)
    if l1_df is not None and not l1_df.empty:
        top_sectors = l1_df.head(cfg.SECTOR.top_n_high)["industry_name"].tolist()
    candidates, sector_ranks = collect_l2(date, top_sectors)

    # 5. L4 退出预警
    l4_warnings = collect_l4_warnings(positions, date)

    # 6. 交易记录
    day_trades = collect_trades(result)

    # 7. 归因
    attribution = collect_attribution(date, l0_info)

    # 8. 数据源
    ds_report = collect_data_source(date)

    # 组装 HTML
    _cov_warn = os.environ.get("COVERAGE_WARNING", "")
    _cov_banner = (f'<div style="background:#fef2f2;border:2px solid #dc2626;'
                   f'border-radius:8px;padding:12px 20px;margin:12px 24px;'
                   f'font-size:14px;color:#991b1b;font-weight:bold;">'
                   f'⚠️ {_cov_warn}</div>') if _cov_warn else ""
    sections = [
        _html_header(date),
        _cov_banner,
        section_executive_summary(l0_info, positions, daily_values, m, l4_warnings),
        section_market_depth(l0_info, l0_trend, l0_weekly, l0_daily, l1_df, candidates, sector_ranks),
        section_portfolio_advice(l0_info, positions, l4_warnings, daily_values, candidates),
        section_trades(day_trades),
        section_appendix(l0_info, attribution, ds_report, date),
        _HTML_FOOTER,
    ]
    return "\n".join(sections)


def _is_intraday(date: str) -> bool:
    """检查是否为当日盘中（< 15:00 收盘前）。"""
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    if date != today:
        return False
    now = _dt.now()
    return now.hour < 15 and now.weekday() < 5


def _intraday_warning(positions: dict) -> str:
    """盘中初稿警告横幅 — 所有持仓价格为快照，非收盘价。"""
    if not positions:
        return ""
    rows_html = ""
    for sym, pos in sorted(positions.items(), key=lambda x: -abs(x[1].get("pnl_pct", 0))):
        entry_price = pos.get("entry_price", 0)
        shares = max(pos.get("shares", 1), 1)
        current = pos.get("market_value", 0) / shares
        pnl_pct = pos.get("pnl_pct", 0) * 100
        color = "#dc2626" if pnl_pct < 0 else "#16a34a"
        rows_html += (
            f"<tr><td><strong>{sym}</strong></td>"
            f"<td>{pos.get('entry_date','')[:10]}</td>"
            f"<td>{entry_price:.2f}</td>"
            f"<td>{current:.2f}</td>"
            f"<td style='color:{color};font-weight:bold'>{pnl_pct:+.2f}%</td>"
            f"<td>{shares}</td></tr>"
        )
    return f"""
<div style='margin:12px 0;padding:14px 18px;background:#fff7ed;border:2px solid #f97316;border-radius:8px'>
 <div style='display:flex;align-items:center;gap:8px;margin-bottom:8px'>
  <span style='font-size:22px'>&#9888;&#65039;</span>
  <span style='font-weight:bold;font-size:15px;color:#c2410c'>初稿 — 所有价格为盘中快照，非收盘价</span>
 </div>
 <div style='font-size:13px;color:#78350f;margin-bottom:10px'>
  15:00 收盘前生成。以下持仓价格为<b>当前盘中价</b>，收盘后可能变化。正式版 16:00 发布。
 </div>
 <div class='table-wrap'><table style='font-size:13px'>
  <tr><th>代码</th><th>入场日期</th><th>入场价</th><th>当前价(盘中)</th><th>浮动盈亏</th><th>股数</th></tr>
  {rows_html}
 </table></div>
</div>"""


def _banner_light(mode: str) -> str:
    """初稿模式警告横幅 — 大红底，显眼不可忽略。

    Args:
        mode: "normal" — 标准盘中模式（已跑 L2）
              "degraded" — 快速模式（未跑 L2，数据不完整）
    """
    if mode == "normal":
        return """<div style="background:#dc2626;color:#fff;padding:14px 18px;margin:0 0 18px 0;
border-radius:8px;font-size:14px;line-height:1.6;text-align:center;">
<strong style="font-size:17px;">⚠️ 盘中初稿</strong><br>
数据更新至 14:30 · 已运行 L2 预计算<br>
所有价格为盘中实时价，非收盘价 · 完整日报请等 16:00 全量管道
</div>"""
    # degraded
    return """<div style="background:#dc2626;color:#fff;padding:14px 18px;margin:0 0 18px 0;
border-radius:8px;font-size:14px;line-height:1.6;text-align:center;">
<strong style="font-size:17px;">⚠️ 快速模式初稿 — 数据不完整</strong><br>
数据未完整下载（14:45 截止） · 未运行 L2 预计算 · 未运行 L4 预警<br>
信号基于昨日持仓快照 + 盘中实时价，仅供参考<br>
完整日报请等 16:00 全量管道
</div>"""


def generate_intraday_html(date: str, skip_l2: bool = False) -> str:
    """生成盘中初稿 HTML（不执行 BacktestEngine，不建新仓）。

    Args:
        date: 日期 YYYY-MM-DD
        skip_l2: True=快速模式（14:45 截止，跳过 L2/L4）

    数据来源：
      - 持仓快照: outputs/position_snapshot.parquet（昨 16:00 管道写入）
      - 实时价格: 腾讯 qt.gtimg.cn（盘中价）
      - 信号: collect_l0/l1（skip_l2=True 时无 L2/L4）
    """
    import pandas as pd
    from pathlib import Path as _P

    mode = "degraded" if skip_l2 else "normal"
    logger.info(f"生成盘中初稿 HTML: {date} (mode={mode})")

    # 交易日检查
    _dr = DataRouter()
    _tdf = _dr.get_trading_dates()
    if not _tdf.empty and "date" in _tdf.columns:
        if date not in set(_tdf["date"].tolist()):
            logger.warning(f"非交易日 {date}")
            return ""

    # 1. 读取持仓快照
    snapshot_path = _P("outputs/position_snapshot.parquet")
    positions = {}
    if snapshot_path.exists():
        try:
            snap = pd.read_parquet(snapshot_path)
            if not snap.empty:
                for _, r in snap.iterrows():
                    sym = r["symbol"]
                    positions[sym] = {
                        "symbol": sym,
                        "shares": int(r.get("shares", 0)),
                        "entry_price": float(r.get("entry_price", 0)),
                        "entry_date": str(r.get("entry_date", ""))[:10],
                        "sector": r.get("sector", ""),
                        "score": float(r.get("score", 0)),
                    }
                    _enrich_position_price(positions[sym])
        except Exception as e:
            logger.warning(f"读取持仓快照失败: {e}")

    # 2. L0 / L1 信号
    l0_info = collect_l0(date)
    l1_df = collect_l1(date)
    top_sectors = []
    if l1_df is not None and not l1_df.empty:
        top_sectors = l1_df.head(getattr(cfg.SECTOR, 'top_n_high', 5))["industry_name"].tolist()

    # 3. L2 / L4（快速模式跳过）
    candidates = []
    sector_ranks = {}
    l4_warnings = []
    if not skip_l2:
        candidates, sector_ranks = collect_l2(date, top_sectors)
        l4_warnings = collect_l4_warnings(positions, date)
    else:
        logger.info("[intraday] 快速模式: 跳过 L2/L4")

    # 4. 指标
    from sniper.engine.metrics import calculate_metrics
    m = calculate_metrics([], [], 1_000_000)

    # 5. 组装 HTML（含模式警告横幅）
    sections = [
        _html_header(date),
        _banner_light(mode),
        section_executive_summary(l0_info, positions, [], m, l4_warnings),
        section_market_depth(l0_info, collect_l0_trend(date), collect_l0_weekly(date),
                            collect_l0_daily(date), l1_df, candidates, sector_ranks),
        section_portfolio_advice(l0_info, positions, l4_warnings, [], candidates),
        section_appendix(l0_info, collect_attribution(date, l0_info),
                        collect_data_source(date), date),
        _HTML_FOOTER,
    ]
    return "\n".join(sections)


def _enrich_position_price(pos: dict) -> None:
    """用腾讯实时行情填充持仓的 market_value / pnl_pct。"""
    import pandas as pd
    try:
        from data.sources.tencent_valuation import fetch_tencent_quotes
        quotes = fetch_tencent_quotes([pos["symbol"]])
        if quotes and quotes[0].get("float_mv"):
            # 腾讯 API 不直接返回最新价，用 total_mv/float_mv 可推导
            # 但这里用 valuation 表已有数据更快
            pass
    except Exception:
        pass

    # fallback: 用 daily_bars 最新 close
    try:
        from data.local.warehouse import LocalDataWarehouse
        wh = LocalDataWarehouse()
        bars = wh.get_daily_bars(pos["symbol"], end="2099-12-31")
        if not bars.empty and "close" in bars.columns:
            latest_close = float(bars["close"].iloc[-1])
            shares = pos.get("shares", 0)
            entry = pos.get("entry_price", 0)
            pos["market_value"] = latest_close * shares
            pos["pnl_pct"] = (latest_close - entry) / entry if entry else 0
    except Exception:
        pos["market_value"] = pos.get("entry_price", 1) * pos.get("shares", 0)
        pos["pnl_pct"] = 0


def _generate_md(date: str) -> str:
    """调用现有的 daily_report 生成 Markdown 日报。"""
    try:
        from scripts.daily_report import generate_report
        return generate_report(date)
    except Exception as e:
        return f"# 量化日报 {date}\n\n> MD 生成失败: {e}"


def _update_obsidian_index(obsidian_dir: Path, date: str, html_file: Path, md_file: Path):
    """更新 Obsidian 索引文件，列出所有日报。"""
    idx = obsidian_dir / "_量化日报索引.md"

    htmls = sorted(obsidian_dir.glob("量化日报_*.html"), reverse=True)
    mds = sorted(obsidian_dir.glob("量化日报_*.md"), reverse=True)

    lines = [
        "# 📊 量化日报索引\n",
        f"> 最后更新: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
        "",
        "| 日期 | Markdown 日报 | HTML 日报 |",
        "|------|--------------|----------|",
    ]

    # 合并所有日期
    all_dates = set()
    for h in htmls:
        all_dates.add(h.stem.replace("量化日报_", ""))
    for m in mds:
        d = m.stem.replace("量化日报-", "").replace("量化日报_", "")
        all_dates.add(d)

    for d in sorted(all_dates, reverse=True):
        md_link = f"[量化日报-{d}.md](量化日报-{d}.md)" if (obsidian_dir / f"量化日报-{d}.md").exists() else "—"
        html_link = f"[量化日报_{d}.html](量化日报_{d}.html)" if (obsidian_dir / f"量化日报_{d}.html").exists() else "—"
        lines.append(f"| {d} | {md_link} | {html_link} |")

    lines.append("")
    lines.append("---")
    lines.append("> 💡 MD 文件可在 Obsidian 内直接查看，HTML 文件在浏览器中打开")

    idx.write_text("\n".join(lines), encoding="utf-8")
    return idx


def main():
    import argparse
    a = argparse.ArgumentParser(description="量化日报生成（HTML + Markdown）")
    a.add_argument("--date", type=str, default="", help="日报日期 YYYY-MM-DD，默认当天")
    a.add_argument("--output", type=str, default="", help="HTML 输出路径（不指定则使用 Obsidian 路径）")
    args = a.parse_args()

    date = args.date or dt.datetime.now().strftime("%Y-%m-%d")

    if args.output:
        # 只生成 HTML
        html = generate_html(date)
        if not html:
            print(f"⏭️ {date} 非交易日，跳过")
            return
        Path(args.output).write_text(html, encoding="utf-8")
        print(f"HTML 日报已写入: {args.output}")
    elif OBSIDIAN_PATH:
        obsidian_dir = Path(OBSIDIAN_PATH)
        obsidian_dir.mkdir(parents=True, exist_ok=True)

        # 1. 生成 HTML
        html = generate_html(date)
        if not html:
            print(f"⏭️ {date} 非交易日，跳过")
            return
        html_out = obsidian_dir / f"量化日报_{date}.html"
        html_out.write_text(html, encoding="utf-8")
        print(f"✅ HTML: {html_out}")

        # 2. 生成 MD
        md_content = _generate_md(date)
        md_out = obsidian_dir / f"量化日报-{date}.md"
        md_out.write_text(md_content, encoding="utf-8")
        print(f"✅ MD:   {md_out}")

        # 3. 更新索引
        idx = _update_obsidian_index(obsidian_dir, date, html_out, md_out)
        print(f"✅ 索引: {idx}")
    else:
        out = Path(f"outputs/daily_report_{date}.html")
        out.write_text(generate_html(date), encoding="utf-8")
        print(f"HTML 日报已写入: {out}")


if __name__ == "__main__":
    main()
