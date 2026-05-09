"""日报 v2 — 三层结构: L1 市场环境 → L2 行业指数 → L3 个股，含卖出信号

用法:
  from reports.daily_report_v2 import generate_v2_report
  md = generate_v2_report(l1_result, l2_result, l3_verdicts, date_str, mode="full")
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DailyReportData:
    """日报数据结构"""
    date: str
    # L1
    l1_state: str = ""
    l1_position_pct: float = 0.0
    l1_strong_count: int = 0
    l1_avg_score: float = 0.0
    l1_yin_die: bool = False
    l1_details: list[str] = field(default_factory=list)
    l1_change: dict = field(default_factory=dict)
    # L2
    l2_strong_sectors: list = field(default_factory=list)
    l2_candidates: list = field(default_factory=list)
    l2_details: list[str] = field(default_factory=list)
    l2_change: dict = field(default_factory=dict)
    # L3
    l3_selections: list = field(default_factory=list)
    l3_exits: list = field(default_factory=list)
    l3_suspended: bool = False
    l3_change: dict = field(default_factory=dict)


def generate_v2_report(data: DailyReportData, mode: str = "full") -> str:
    """生成新版三层 Markdown 日报。

    mode: "push" (推送版) | "full" (完整版)
    """
    if mode == "push":
        return _generate_push(data)
    return _generate_full(data)


def _generate_push(data: DailyReportData) -> str:
    """推送版: 摘要栏 + L1评论 + L2 Top5 + L3 Top3 + 警报"""
    lines = []
    lines.append(f"## 量化日报 {data.date}")
    lines.append("")

    # 摘要栏
    state_icon = {"牛市": "🟢", "震荡": "🟡", "偏弱": "🟠", "熊市": "🔴"}.get(data.l1_state, "⚪")
    l3_status = "⏸暂停" if data.l3_suspended else "▶运行"
    yin_die_tag = "⚠阴跌" if data.l1_yin_die else ""
    lines.append(f"> {state_icon} **{data.l1_state}** | 仓位{data.l1_position_pct*100:.0f}% | "
                 f"L2强势{len(data.l2_strong_sectors)} | L3{l3_status} {yin_die_tag}")
    lines.append("")

    # L1 评论
    lines.append("### L1 市场环境")
    lines.append(f"状态: {data.l1_state} ({data.l1_strong_count}/3市强)")
    lines.append(f"均分: {data.l1_avg_score:.1f} | 目标仓位: {data.l1_position_pct*100:.0f}%")
    if data.l1_yin_die:
        lines.append("> ⚠ 阴跌触发，L3 暂停")
    if data.l1_change:
        lines.append(f"变化: {data.l1_change.get('prev_state', '—')} → {data.l1_state}")
    lines.append("")

    # L2 Top5
    lines.append("### L2 强势指数 Top5")
    if data.l2_strong_sectors:
        for i, s in enumerate(data.l2_strong_sectors[:5], 1):
            name = s if isinstance(s, str) else getattr(s, "etf_name", str(s))
            score = "" if isinstance(s, str) else f" | {getattr(s, 'score', 0):.0f}分"
            lines.append(f"{i}. {name}{score}")
    else:
        lines.append("— 无强势指数")
    lines.append("")

    # L3 Top3
    lines.append("### L3 推荐个股 Top3")
    if data.l3_suspended:
        lines.append("> L3 暂停中（阴跌）")
    elif data.l3_selections:
        for i, sv in enumerate(data.l3_selections[:3], 1):
            name = sv if isinstance(sv, str) else getattr(sv, "symbol", str(sv))
            score = "" if isinstance(sv, str) else f" | {getattr(sv, 'score', 0):.0f}分"
            pat = "" if isinstance(sv, str) else f" [{getattr(sv, 'pattern_category', '')}]"
            lines.append(f"{i}. {name}{score}{pat}")
    else:
        lines.append("— 无推荐")
    lines.append("")

    # 警报
    if data.l3_exits:
        lines.append("### ⚠ 卖出信号")
        for ex in data.l3_exits[:3]:
            signal_text = str(ex) if isinstance(ex, str) else ex.get("action", str(ex))
            lines.append(f"- {signal_text}")

    return "\n".join(lines)


def _generate_full(data: DailyReportData) -> str:
    """完整版: 全部数据 + 全部排名 + 卖出信号详情"""
    lines = []
    lines.append(f"# 量化日报 — {data.date}")
    lines.append("")

    # ── L1 市场环境 ──
    lines.append("## 一、L1 市场环境")
    lines.append("")
    state_icon = {"牛市": "🟢", "震荡": "🟡", "偏弱": "🟠", "熊市": "🔴"}.get(data.l1_state, "⚪")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 市场状态 | {state_icon} {data.l1_state} |")
    lines.append(f"| 强势市场 | {data.l1_strong_count}/3 |")
    lines.append(f"| 目标仓位 | {data.l1_position_pct*100:.0f}% |")
    lines.append(f"| 三市均分 | {data.l1_avg_score:.1f} |")
    lines.append(f"| 阴跌 | {'⚠ 是' if data.l1_yin_die else '否'} |")
    if data.l1_change:
        prev_state = data.l1_change.get("prev_state", "—")
        lines.append(f"| 上次状态 | {prev_state} → {data.l1_state} |")
    lines.append("")

    if data.l1_details:
        lines.append("### 各市场详情")
        for d in data.l1_details:
            lines.append(f"- {d}")
        lines.append("")

    # ── L2 行业指数 ──
    lines.append("## 二、L2 行业指数")
    lines.append("")

    if data.l2_strong_sectors:
        lines.append("| # | 指数 | 评分 | 资金 | 风险 |")
        lines.append("|---|------|------|------|------|")
        for i, s in enumerate(data.l2_strong_sectors, 1):
            if isinstance(s, str):
                lines.append(f"| {i} | {s} | — | — | — |")
            else:
                name = getattr(s, "etf_name", str(s))
                score = f"{getattr(s, 'score', 0):.1f}"
                fund = getattr(s, "fund_level", "—")
                risk = "⚠" if getattr(s, "risk_warning", False) else "✓"
                lines.append(f"| {i} | {name} | {score} | {fund} | {risk} |")
    else:
        lines.append("— 无强势指数")
    lines.append("")

    if data.l2_candidates:
        lines.append(f"候选: {len(data.l2_candidates)} | 强势: {len(data.l2_strong_sectors)}")
        lines.append("")

    # ── L3 个股 ──
    lines.append("## 三、L3 个股")
    lines.append("")

    if data.l3_suspended:
        lines.append("> ⚠ L3 暂停中（阴跌触发）")
        lines.append("")
    elif data.l3_selections:
        lines.append("| # | 代码 | 评分 | 趋势 | Alpha | 风险 | 形态 | 类别 |")
        lines.append("|---|------|------|------|-------|------|------|------|")
        for i, sv in enumerate(data.l3_selections, 1):
            if isinstance(sv, str):
                lines.append(f"| {i} | {sv} | — | — | — | — | — | — |")
            else:
                sym = getattr(sv, "symbol", "—")
                score = f"{getattr(sv, 'score', 0):.1f}"
                trend = f"{getattr(sv, 'trend_score', 0):.1f}"
                alpha = f"{getattr(sv, 'alpha_score', 0):.1f}"
                risk = f"{getattr(sv, 'risk_score', 0):.1f}"
                pat_labels = ", ".join(getattr(sv, "pattern_labels", []))
                pat_cat = getattr(sv, "pattern_category", "—")
                lines.append(f"| {i} | {sym} | {score} | {trend} | {alpha} | {risk} | {pat_labels} | {pat_cat} |")
    else:
        lines.append("— 无推荐个股")
    lines.append("")

    # ── 卖出信号 ──
    if data.l3_exits:
        lines.append("### 卖出信号")
        lines.append("")
        lines.append("| 代码 | 信号 | 操作 |")
        lines.append("|------|------|------|")
        for ex in data.l3_exits:
            if isinstance(ex, str):
                lines.append(f"| — | {ex} | — |")
            else:
                sym = ex.get("symbol", "—")
                sigs = ", ".join(ex.get("signals", []))
                action = ex.get("action", "—")
                lines.append(f"| {sym} | {sigs} | {action} |")
        lines.append("")

    # ── 建议 ──
    lines.append("### 操作建议")
    lines.append("")
    suggestions = _generate_suggestions(data)
    for s in suggestions:
        lines.append(f"- {s}")

    return "\n".join(lines)


def _generate_suggestions(data: DailyReportData) -> list[str]:
    """生成操作建议"""
    suggestions = []

    # L1 状态建议
    if data.l1_state == "牛市":
        suggestions.append("✅ 建议仓位 100%: 全面做多，动量因子优先")
    elif data.l1_state == "震荡":
        suggestions.append("🟡 建议仓位 70%: 高抛低吸，均衡配置")
    elif data.l1_state == "偏弱":
        suggestions.append("🟠 建议仓位 30%: 反转因子优先，严格止损")
    else:
        suggestions.append("🔴 建议仓位 0%: 空仓等待，关注阴跌恢复")

    # 阴跌
    if data.l1_yin_die:
        suggestions.append("⚠ 阴跌触发: L3 选股暂停，持有仓位密切跟踪")

    # L2
    if not data.l2_strong_sectors:
        suggestions.append("⚠ L2 无强势指数: 暂停新买入")
    elif len(data.l2_strong_sectors) <= 2:
        suggestions.append(f"⚠ L2 仅{len(data.l2_strong_sectors)}个强势指数: 集中配置，控制仓位")

    # L3
    if data.l3_exits:
        exit_signal_types = set()
        for ex in data.l3_exits:
            sigs = ex.get("signals", []) if isinstance(ex, dict) else []
            exit_signal_types.update(sigs)
        suggestions.append(f"📉 触发卖出信号: {', '.join(exit_signal_types)}")

    return suggestions
