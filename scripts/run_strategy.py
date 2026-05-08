"""量化日报 — L1→L2→L3 硬过滤管道 + Obsidian 输出"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime

import pandas as pd
import numpy as np

from backtest.data_loader import load_all_from_cache, get_benchmark
from core.logger import get_logger

logger = get_logger("scripts.run_strategy")

CACHE_DIR = Path("data/raw/_cache/backtest")
OBSIDIAN_DIR = Path("D:/Obsidian/SecondBrain/03-Areas/量化系统")
OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)

MODEL_VERSION = "3.0.0"

INDEX_CN = {
    "shanghai": "上证指数",
    "shenzhen": "深证成指",
    "chinext": "创业板指",
    "csi300": "沪深300",
}

# 独立强势股配置
INDEPENDENT_STOCK_THRESHOLD = 75.0  # L3评分最低要求
INDEPENDENT_STOCK_CHAN_SCORE = 25   # 缠论买点满分要求
INDEPENDENT_STOCK_SLOTS = 1         # 名额


# ═══════════════════════ ETF 标签计算 ═══════════════════════

ETF_TAGS_CACHE = CACHE_DIR / "etf_tags.parquet"


def _load_etf_tags_map(stock_names: dict[str, str]) -> dict[str, list[str]]:
    """加载 ETF 标签映射（优先读缓存，失效时重建）"""
    import time

    # 读缓存（TTL 30天，行业分类变化极慢）
    if ETF_TAGS_CACHE.exists():
        age = time.time() - ETF_TAGS_CACHE.stat().st_mtime
        if age < 30 * 86400:
            df = pd.read_parquet(ETF_TAGS_CACHE)
            result: dict[str, list[str]] = {}
            for _, row in df.iterrows():
                tags = row["etf_tags"]
                if isinstance(tags, (list, np.ndarray)):
                    result[row["symbol"]] = list(tags)
                else:
                    result[row["symbol"]] = []
            return result

    # 重建
    logger.info("重建 ETF 标签映射...")
    try:
        from gate.sector_mapper import SectorMapper
        from data.industry import build_symbol_industry_map, build_symbol_concepts_map

        mapper = SectorMapper()
        industry_map = build_symbol_industry_map()
        concept_map = build_symbol_concepts_map()

        result = {}
        for symbol in stock_names:
            industry = industry_map.get(symbol)
            concepts = concept_map.get(symbol, [])
            tags = mapper.map_stock_to_etf(
                symbol_industry=industry,
                symbol_concepts=concepts if concepts else None,
            )
            result[symbol] = tags

        # 持久化
        rows = [{"symbol": s, "etf_tags": t} for s, t in result.items()]
        pd.DataFrame(rows).to_parquet(ETF_TAGS_CACHE, index=False)
        logger.info(f"ETF 标签已缓存: {ETF_TAGS_CACHE}")
        return result
    except Exception as e:
        logger.warning(f"ETF 标签构建失败，使用空标签: {e}")
        return {}


# ═══════════════════════ 评分子项计算 ═══════════════════════

def _compute_sub_scores(top_stocks: pd.DataFrame, stock_data: dict) -> pd.DataFrame:
    """为 top 股票计算统一维度细分得分（趋势/Alpha/量能/资金）"""
    from factors.macd import calc_macd
    from factors.volume import score_volume_three_part

    df = top_stocks.copy()
    trend_scores = []
    alpha_scores = []
    volume_scores = []
    fund_scores = []

    for _, row in df.iterrows():
        sym = row["symbol"]
        daily = stock_data.get(sym)
        if daily is None or daily.empty or len(daily) < 60:
            trend_scores.append(15)
            alpha_scores.append(12)
            volume_scores.append(15)
            fund_scores.append(12)
            continue

        close = daily["close"]
        volume = daily["volume"]

        # 趋势分 (0-30): 简化MACD评估
        try:
            dif, dea, hist = calc_macd(close)
            dif_above_dea = float((dif > dea).iloc[-1])
            dif_above_zero = float((dif > 0).iloc[-1])
            hist_positive = float((hist > 0).iloc[-1])
            hist_trend = float((hist > hist.shift(1)).tail(5).mean()) if len(hist) > 5 else 0
            trend = round(dif_above_dea * 12 + dif_above_zero * 8 + hist_positive * 5 + hist_trend * 5, 1)
        except Exception:
            trend = 15
        trend_scores.append(min(30, trend))

        # Alpha分 (0-25): 月内涨幅 + 价格位置
        try:
            monthly_ret = float(close.iloc[-1] / close.iloc[0] - 1) if len(close) > 1 else 0
            hl_range = float(daily["high"].max() - daily["low"].min())
            range_pos = float((close.iloc[-1] - daily["low"].min()) / hl_range) if hl_range > 0 else 0.5
            alpha = round(max(0, min(25, monthly_ret * 100 * 0.4 + range_pos * 10)), 1)
        except Exception:
            alpha = 12
        alpha_scores.append(alpha)

        # 量能分 (0-25): 三部分法缩放
        try:
            vol_health = score_volume_three_part(volume, close)
            vol = round(vol_health * 0.25, 1)
        except Exception:
            vol = 12.5
        volume_scores.append(min(25, vol))

        # 资金分 (0-20): 基于量价的技术推断
        try:
            vol_ok = vol >= 12.5
            dif_ok = bool((calc_macd(close)[0] > calc_macd(close)[1]).iloc[-1])
            if vol_ok and dif_ok:
                fund = 16.0
            elif vol_ok:
                fund = 12.0
            elif dif_ok:
                fund = 10.0
            else:
                fund = 6.0
        except Exception:
            fund = 10
        fund_scores.append(fund)

    df["趋势分"] = trend_scores
    df["Alpha分"] = alpha_scores
    df["量能分"] = volume_scores
    df["资金分"] = fund_scores
    return df


# ═══════════════════════ 数据辅助 ═══════════════════════

def _load_stock_names() -> dict[str, str]:
    """加载股票代码→名称映射"""
    name_path = CACHE_DIR / "stock_names.parquet"
    if name_path.exists():
        df = pd.read_parquet(name_path)
        return dict(zip(df["symbol"], df["name"]))
    try:
        from data.downloader import DataDownloader
        dl = DataDownloader()
        sl = dl.fetch_stock_list()
        if sl is not None and not sl.empty:
            names = dict(zip(sl["symbol"], sl["name"]))
            pd.DataFrame({"symbol": list(names.keys()), "name": list(names.values())}).to_parquet(name_path, index=False)
            return names
    except Exception as e:
        logger.warning(f"获取股票名称失败: {e}")
    return {}


def _get_market_indices(market_daily: dict) -> list[dict]:
    """各指数最新行情 + 年化波动率"""
    rows = []
    for name, df in market_daily.items():
        if df.empty or len(df) < 2:
            continue
        try:
            cn = INDEX_CN.get(name, name)
            close = float(df["close"].iloc[-1])
            prev = float(df["close"].iloc[-2])
            pct = close / prev - 1

            rets = df["close"].pct_change().dropna().tail(20)
            vol20 = float(rets.std() * np.sqrt(252)) if len(rets) > 1 else 0

            rows.append({"name": cn, "close": close, "pct": pct, "vol20": vol20})
        except Exception:
            continue
    return rows


def _sector_recommendation(score: float, gate_passed: bool) -> str:
    """根据L2 Gate评分给出行业建议"""
    if not gate_passed:
        return "不通过"
    if score >= 75:
        return "重点关注"
    elif score >= 60:
        return "关注"
    elif score >= 45:
        return "持有"
    elif score >= 30:
        return "观望"
    else:
        return "回避"


def _get_sector_rankings_from_gate(etf_weekly: dict, benchmark_close,
                                    l1_market_state: str = "volatile") -> tuple[list[dict], set[str]]:
    """
    使用 L2 Gate 系统获取行业排名。

    返回: (rankings_list, strong_sector_names_set)
    """
    from gate.layer2_sector import assess_sectors

    result = assess_sectors(
        etf_data=etf_weekly,
        benchmark_close=benchmark_close,
        l1_market_state=l1_market_state,
    )

    strong_names = {v.etf_name for v in result.strong_sectors}

    rows = []
    for v in result.candidate_sectors:
        rows.append({
            "name": v.etf_name,
            "score": v.score,
            "passed": v.passed_gate,
            "bullish": v.bullish_score,
            "bearish": v.bearish_score,
            "fund_level": v.fund_level,
            "risk_warning": v.risk_warning,
            "recommend": _sector_recommendation(v.score, v.passed_gate),
        })

    # 未通过门闸的也列出（供参考）
    all_etfs = set()
    for v in result.candidate_sectors:
        all_etfs.add(v.etf_name)

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows, strong_names


def _get_concept_board_rankings(top_n: int = 8) -> list[dict]:
    """获取概念板块涨跌幅排名（快速单次 API 调用）"""
    try:
        import akshare as ak
        df = ak.stock_board_concept_name_em()
        if df is None or df.empty:
            return []

        # 列位置: 0=排名,1=名称,2=代码,3=最新价,4=涨跌幅,5=涨跌额
        rows = []
        for _, row in df.iterrows():
            try:
                name = str(row.iloc[1])
                pct = float(row.iloc[5])  # 涨跌幅（已是百分比数值）
                rows.append({"name": name, "pct": pct})
            except (ValueError, IndexError):
                continue

        rows.sort(key=lambda r: r["pct"], reverse=True)
        return rows[:top_n]
    except Exception as e:
        logger.warning(f"获取概念板块排名失败: {e}")
        return []


def _get_sector_rankings_fallback(etf_weekly: dict) -> list[dict]:
    """降级方案: 简单动量排名（L2 Gate 不可用时）"""
    rows = []
    for name, df in etf_weekly.items():
        if df.empty or len(df) < 5:
            continue
        try:
            closes = df["close"]
            ret_4w = closes.iloc[-1] / closes.iloc[-4] - 1 if len(closes) >= 4 else 0
            ret_12w = closes.iloc[-1] / closes.iloc[-12] - 1 if len(closes) >= 12 else 0
            vol = closes.pct_change().dropna().std() * np.sqrt(52) if len(closes) > 1 else 0
            recommend = "关注" if ret_4w > 0.05 and ret_12w > 0.05 else ("持有" if ret_4w > 0 else "观望")
            rows.append({
                "name": name, "score": ret_4w * 100, "ret_4w": ret_4w, "ret_12w": ret_12w,
                "vol": vol, "recommend": recommend, "passed": True,
                "bullish": 0, "bearish": 0, "fund_level": "N/A", "risk_warning": False,
            })
        except Exception:
            continue
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def _get_top_stocks(l3_scores: pd.DataFrame, stock_names: dict, stock_data: dict,
                    strong_sectors: set[str] | None = None,
                    top_n: int = 15) -> tuple[pd.DataFrame, int]:
    """
    当前月 L3 评分, 优先选强势行业池内股票。

    流程:
    1. 先筛选属于强势行业的股票
    2. 按 L3 评分取 top N
    3. 独立强势股例外: 即使行业不在强池，评分 >75 且缠论满分 → 可入选

    Returns:
        (DataFrame, total_strong_pool_count) — DF 为 TopN 选股结果，int 为强势池股票总数
    """
    if l3_scores is None or l3_scores.empty:
        return pd.DataFrame(), 0
    latest_month = l3_scores["month"].max()
    month_data = l3_scores[l3_scores["month"] == latest_month].copy()
    month_data = month_data[month_data["passed"]].copy()

    if month_data.empty:
        return month_data, 0

    etf_map = _load_etf_tags_map(stock_names)

    # 标记行业归属
    month_data["行业"] = month_data["symbol"].map(etf_map).apply(
        lambda tags: list(tags) if tags else []
    )

    if strong_sectors:
        # 判断是否在强势行业中
        def in_strong_pool(tags):
            if not tags:
                return False
            return bool(set(tags) & strong_sectors)

        def is_independent(row):
            """独立强势股: 评分高 + 缠论买点满分 + 即使不在强池"""
            score = row.get("score", 0)
            chan = row.get("chan_buy_score", 0)
            tags = row.get("行业", [])
            if score >= INDEPENDENT_STOCK_THRESHOLD and chan >= INDEPENDENT_STOCK_CHAN_SCORE:
                if not tags or not (set(tags) & strong_sectors):
                    return True
            return False

        month_data["in_strong_pool"] = month_data["行业"].apply(in_strong_pool)
        month_data["is_independent"] = month_data.apply(is_independent, axis=1)

        # 在 TopN 截断前统计强势池股票总数
        total_scored = int(month_data["in_strong_pool"].sum())

        # 主力: 强势行业池内股票
        pool_stocks = month_data[month_data["in_strong_pool"]].nlargest(top_n, "score")
        # 独立强势股补充 (最多 INDEPENDENT_STOCK_SLOTS 只)
        independent = month_data[month_data["is_independent"]].nlargest(INDEPENDENT_STOCK_SLOTS, "score")

        # 合并去重
        selected = pd.concat([pool_stocks, independent]).drop_duplicates(subset=["symbol"])
        if len(selected) < top_n:
            # 不足时从剩余股票补足
            rest = month_data[~month_data["symbol"].isin(selected["symbol"])].nlargest(top_n - len(selected), "score")
            selected = pd.concat([selected, rest])
        month_data = selected.head(top_n)
    else:
        total_scored = len(month_data)
        month_data = month_data.nlargest(top_n, "score")

    month_data["name"] = month_data["symbol"].map(stock_names).fillna("")
    month_data["行业_display"] = month_data["行业"].apply(
        lambda tags: "、".join(tags) if tags else "综合"
    )

    # 细分得分
    month_data = _compute_sub_scores(month_data, stock_data)

    month_data = month_data.reset_index(drop=True)
    return month_data, total_scored


# ═══════════════════════ Markdown 报告 ═══════════════════════

def generate_markdown(market_indices, sector_rankings, top_stocks, today_str: str,
                       total_scored: int = 0, l1_result=None, cross_validation=None,
                       strong_sectors: set[str] | None = None,
                       concept_rankings: list | None = None) -> str:
    """生成 Obsidian Markdown — McKinsey 金字塔结构 + 暖色调排版"""

    # ── 金色调色板 (整体金色背景 + 深蓝字点缀) ──
    C = {
        "brand_navy": "#1e3a5f",
        "brand_navy2": "#2d5a87",
        "gold": "#c4a87c",
        "gold_light": "#f5edd0",
        "page_bg": "#f8f0d0",      # 页面整体金色背景
        "bg_card": "#faf5e0",      # 卡片背景（略浅于页面）
        "bg_section": "#f5ebc8",   # 区块背景（略深于页面增加层次）
        "border": "#e8d8a8",
        "border_light": "#f0e8c0",
        "text_body": "#4b463f",
        "text_sec": "#7a7568",
        "text_muted": "#a09888",
        "text_dark": "#2d2820",
        "green": "#8b9d6b",
        "green_bg": "#eef3e6",
        "red": "#c44536",
        "red_bg": "#fdf0ed",
        "amber": "#c49a6c",
        "amber_bg": "#f8edd0",
        "purple": "#8b7ca8",
        "white": "#ffffff",
    }

    _S = '<span style="font-family:宋体;font-size:10.5pt">'
    _B = '<span style="font-family:宋体;font-size:10.5pt;font-weight:bold">'
    _E = '</span>'

    def T(tag, style, content):
        return f'<{tag} style="{style}">{content}</{tag}>'

    def stat_card(title, value, color=C["text_dark"], sub="",
                  val_size="16pt"):
        return (
            f'<td style="padding:14px 16px;border:1px solid {C["border"]};'
            f'border-radius:4px;width:25%;min-width:120px;background:{C["bg_card"]};">'
            f'<div style="color:{C["text_sec"]};font-family:宋体;font-size:8pt;'
            f'margin-bottom:4px;letter-spacing:0.5px;">{title}</div>'
            f'<div style="color:{color};font-family:宋体;font-size:{val_size};'
            f'font-weight:bold;">{value}</div>'
            + (f'<div style="color:{C["text_muted"]};font-family:宋体;font-size:8pt;'
               f'margin-top:2px;">{sub}</div>' if sub else "")
            + '</td>'
        )

    lines = []

    # ── Frontmatter ──
    lines.append("---")
    lines.append(f"date: {today_str}")
    lines.append("type: quant-report")
    lines.append("tags: [量化, 日报]")
    lines.append("---")
    lines.append("")

    # ═══════════════════ HEADER BANNER ═══════════════════
    lines.append(
        f'<div style="background:linear-gradient(135deg,{C["brand_navy"]},{C["brand_navy2"]});'
        f'padding:22px 28px;border-radius:6px;margin-bottom:6px;">'
    )
    lines.append(
        f'<div style="color:{C["gold"]};font-family:宋体;font-size:9pt;'
        f'margin-bottom:2px;letter-spacing:1px;">量化研究部 · 每日市场监测</div>'
    )
    lines.append(
        f'<h1 style="color:{C["white"]};font-family:宋体;font-size:22pt;'
        f'margin:4px 0 2px 0;letter-spacing:1px;">{today_str} 量化数据日报</h1>'
    )
    lines.append(
        f'<div style="color:{C["gold_light"]};font-family:宋体;font-size:9pt;'
        f'margin-top:4px;">模型版本 V{MODEL_VERSION} · 三层漏斗统一评分 · 数据截止前一交易日收盘</div>'
    )
    lines.append('</div>')
    lines.append("")

    # ═══════════════════ EXECUTIVE SUMMARY ═══════════════════
    summary_cells = []
    if l1_result:
        pos = l1_result.actual_position_pct * 100
        state = l1_result.market_state
        state_color = {"牛市": C["green"], "震荡": C["brand_navy"],
                       "偏弱": C["amber"], "熊市": C["red"]}.get(state, C["text_body"])
        summary_cells.append(stat_card("大盘状态", state, state_color))
        summary_cells.append(
            stat_card("综合均分", f'{l1_result.avg_score:.1f}', C["brand_navy"])
        )
        summary_cells.append(
            stat_card("建议仓位", f'{pos:.0f}%', C["brand_navy"])
        )
    if sector_rankings:
        top_sec = sector_rankings[0]
        summary_cells.append(
            stat_card("最强行业", top_sec["name"], C["green"],
                      f'评分 {top_sec["score"]:.1f}', "14pt")
        )
    if strong_sectors:
        summary_cells.append(
            stat_card("强势行业池", "、".join(sorted(strong_sectors)),
                      C["brand_navy"], val_size="10pt")
        )

    if summary_cells:
        rows_html = ""
        chunks = [summary_cells[i:i+4] for i in range(0, len(summary_cells), 4)]
        for chunk in chunks:
            rows_html += "<tr>" + "".join(chunk) + "</tr>"
        lines.append(
            f'<table style="border-collapse:collapse;width:100%;margin:16px 0;">'
            f'{rows_html}</table>'
        )
        lines.append("")

    # ═══════════════════ L1: 市场环境 ═══════════════════════
    # Funnel order: L1 (一) -> L2 (二) -> L3 (三) ::: macro -> sector -> stock
    # McKinsey 金字塔原则：结论先行 → 支撑数据 → 行动建议
    lines.append(
        T("h2",
          f'color:{C["brand_navy"]};font-family:宋体;font-size:14pt;'
          f'border-bottom:2px solid {C["brand_navy"]};padding-bottom:4px;'
          f'margin:20px 0 12px 0;',
          "一、市场环境 (L1)")
    )
    lines.append("")

    if l1_result:
        pos = l1_result.actual_position_pct * 100
        state = l1_result.market_state
        avg = l1_result.avg_score
        strong_cnt = l1_result.strong_count
        bottom_cnt = l1_result.bottom_bullish_markets

        # McKinsey 式核心观点 — 一句话结论
        state_desc_map = {
            "牛市": "市场处于强势牛市环境，各指数多头排列明确，赚钱效应显著。",
            "震荡": "市场处于震荡格局，多空力量相对均衡，结构性机会为主。",
            "偏弱": "市场上行动能不足，缺乏明确的趋势性机会，需控制仓位、防御为主。",
            "熊市": "市场处于弱势熊市环境，系统性风险较高，建议空仓观望等待企稳。",
        }
        core_insight = state_desc_map.get(state, "市场环境复杂，需谨慎对待。")

        # 支撑数据
        if bottom_cnt == 0:
            support_data = (
                f"三大指数中 {strong_cnt}/3 处于强势状态，但无底部共振信号（{bottom_cnt}/3），"
                f"表明多头力量尚未形成合力。"
            )
        else:
            support_data = (
                f"三大指数中 {strong_cnt}/3 处于强势状态，{bottom_cnt}/3 出现底部共振信号，"
                f"市场存在结构性机会。"
            )

        # 行动建议
        action = f"建议仓位控制在 {pos:.0f}%"
        if pos <= 20:
            action += "，以防御性配置为主，耐心等待市场企稳信号。"
        elif pos <= 50:
            action += "，适度参与结构性行情，注意控制回撤风险。"
        else:
            action += "，积极参与市场行情，顺势而为。"
        if state in ("偏弱", "熊市"):
            action += " 操作上以低吸为主，避免追高。"
        if l1_result.risk_warning:
            action += " 部分指数已出现顶部信号，多头仓位已施加折扣。"

        full_desc = f"{core_insight} {support_data} {action}"
        state_color = {"牛市": C["green"], "震荡": C["brand_navy"],
                       "偏弱": C["amber"], "熊市": C["red"]}.get(state, C["text_body"])

        # 核心观点 callout（暖色调背景）
        lines.append(
            f'<div style="background:{C["bg_section"]};'
            f'border:1px solid {C["border"]};'
            f'border-left:4px solid {state_color};'
            f'padding:14px 18px;margin:8px 0;border-radius:3px;">'
            f'<div style="color:{C["text_dark"]};font-family:宋体;font-size:10.5pt;'
            f'line-height:1.8;">{full_desc}</div>'
            f'</div>'
        )
        lines.append("")

        # 核心指标表
        lines.append(
            T("h3",
              f'color:{C["text_dark"]};font-family:宋体;font-size:12pt;'
              f'margin:16px 0 8px 0;',
              "1.1 市场核心指标")
        )
        lines.append("")
        gate_mode = {"牛市": "3-of-5", "震荡": "2-of-5",
                     "偏弱": "2-of-5", "熊市": "跳过L2"}.get(state, "2-of-5")
        lines.append("| 指标 | 数值 | 解读 |")
        lines.append("|:---|---:|:---|")
        lines.append(f"| 大盘状态 | **{state}** | 综合评分 {avg:.1f}，{strong_cnt}/3 指数强势 |")
        lines.append(f"| 建议仓位 | **{pos:.0f}%** | L1 Gate 输出，动态风险调整 |")
        lines.append(f"| L2 Gate | **{gate_mode}** | 与 L1 状态联动，控制行业筛选严格度 |")
        lines.append(f"| 底部共振 | {bottom_cnt}/3 | 三大指数同步底部信号计数 |")
        lines.append("")
    else:
        lines.append(
            f'<div style="color:{C["text_muted"]};font-family:宋体;'
            f'font-size:10.5pt;padding:8px 0;">L1 市场评估数据不可用。</div>'
        )
        lines.append("")

    # 指数表
    lines.append(
        T("h3",
          f'color:{C["text_dark"]};font-family:宋体;font-size:12pt;'
          f'margin:16px 0 8px 0;',
          "1.2 主要指数表现")
    )
    lines.append("")
    lines.append("| 指数名称 | 收盘价 | 涨跌幅 | 年化波动率 |")
    lines.append("|:---|---:|---:|---:|")
    for idx in market_indices:
        pct_clr = C["green"] if idx["pct"] >= 0 else C["red"]
        pct_str = f'<span style="color:{pct_clr};font-weight:bold;">{idx["pct"]:+.2%}</span>'
        lines.append(f"| {idx['name']} | {idx['close']:.2f} | {pct_str} | {idx['vol20']:.1%} |")
    lines.append("")
    lines.append(
        f'<div style="color:{C["text_muted"]};font-family:宋体;font-size:8pt;'
        f'margin-bottom:12px;">数据来源：AKShare / 交易所公开数据</div>'
    )
    lines.append("")

    # ═══════════════════ L2: 行业板块 ═══════════════════
    # 金字塔原则：先给出板块格局判断
    top_sector_name = sector_rankings[0]["name"] if sector_rankings else "—"
    top_sector_score = sector_rankings[0]["score"] if sector_rankings else 0
    sector_insight = (
        f"行业分化明显，{top_sector_name}（{top_sector_score:.1f}分）领跑，"
        f"强势行业集中在 {len(strong_sectors) if strong_sectors else 0} 个板块，"
        f"建议聚焦龙头板块进行选股。"
    )
    lines.append(
        T("h2",
          f'color:{C["brand_navy"]};font-family:宋体;font-size:14pt;'
          f'border-bottom:2px solid {C["brand_navy"]};padding-bottom:4px;'
          f'margin:20px 0 12px 0;',
          "二、行业板块 (L2)")
    )
    lines.append("")

    # 行业核心观点 callout
    lines.append(
        f'<div style="background:{C["bg_section"]};'
        f'border:1px solid {C["border"]};'
        f'border-left:4px solid {C["gold"]};'
        f'padding:12px 16px;margin:8px 0 16px 0;border-radius:3px;">'
        f'<div style="color:{C["text_sec"]};font-family:宋体;font-size:9pt;'
        f'margin-bottom:2px;">核心判断</div>'
        f'<div style="color:{C["text_dark"]};font-family:宋体;font-size:10.5pt;'
        f'font-weight:bold;">{sector_insight}</div>'
        f'</div>'
    )
    lines.append("")

    # 2.1 Gate 排名
    lines.append(
        T("h3",
          f'color:{C["text_dark"]};font-family:宋体;font-size:12pt;'
          f'margin:16px 0 8px 0;',
          "2.1 L2 Gate 行业排名")
    )
    lines.append("")
    lines.append(
        f'<div style="color:{C["text_sec"]};font-family:宋体;font-size:9pt;'
        f'margin-bottom:8px;">基于四维度评分（趋势+Alpha+量能+资金）经 Gate 筛选。</div>'
    )
    lines.append("")
    lines.append("| 排名 | 行业 | Gate评分 | 多头 | 空头 | 资金 | 风险 | 建议 |")
    lines.append("|---:|:---|---:|---:|---:|:---|:---|:---|")
    for i, sec in enumerate(sector_rankings, 1):
        risk_mark = f'<span style="color:{C["red"]};">⚠</span>' if sec.get("risk_warning") else ""
        lines.append(
            f"| {i} | {sec['name']} | **{sec['score']:.1f}** | {sec.get('bullish', 0)}/5 | "
            f"{sec.get('bearish', 0)}/3 | {sec.get('fund_level', 'N/A')} | {risk_mark} | "
            f"{sec['recommend']} |"
        )
    lines.append("")

    # 2.2 强势行业池
    if strong_sectors:
        tag_html = "".join(
            f'<span style="display:inline-block;background:{C["green_bg"]};'
            f'color:{C["green"]};font-family:宋体;font-size:9pt;font-weight:bold;'
            f'padding:2px 10px;border-radius:3px;margin:2px 3px;">{s}</span>'
            for s in sorted(strong_sectors)
        )
        lines.append(
            T("h3",
              f'color:{C["text_dark"]};font-family:宋体;font-size:12pt;'
              f'margin:16px 0 8px 0;',
              "2.2 当前强势行业池")
        )
        lines.append("")
        lines.append(f'<div style="margin:6px 0;">{tag_html}</div>')
        lines.append("")
        lines.append(
            f'<div style="color:{C["text_sec"]};font-family:宋体;font-size:9pt;">'
            f'L3 选股优先从以上强势行业中选取，不在池中的标的以「补位」标记。</div>'
        )
        lines.append("")

    # 2.3 交叉验证
    if cross_validation:
        lines.append(
            T("h3",
              f'color:{C["text_dark"]};font-family:宋体;font-size:12pt;'
              f'margin:16px 0 8px 0;',
              "2.3 申万行业交叉验证")
        )
        lines.append("")
        lines.append("| 申万行业 | 行业强度 | 与Gate方向 |")
        lines.append("|:---|---:|:---|")
        for cv in cross_validation:
            converge_clr = C["green"] if cv["converge"] else C["red"]
            mark = f'<span style="color:{converge_clr};">{"✓ 一致" if cv["converge"] else "⚠ 背离"}</span>'
            lines.append(f"| {cv['sw_name']} | {cv['sw_score']:.1f} | {mark} |")
        lines.append("")

    # 2.4 概念板块
    if concept_rankings:
        lines.append(
            T("h3",
              f'color:{C["text_dark"]};font-family:宋体;font-size:12pt;'
              f'margin:16px 0 8px 0;',
              "2.4 概念板块涨幅榜")
        )
        lines.append("")
        lines.append("| 排名 | 概念板块 | 涨跌幅 |")
        lines.append("|---:|:---|---:|")
        for i, cb in enumerate(concept_rankings, 1):
            pct_clr = C["green"] if cb["pct"] >= 0 else C["red"]
            lines.append(
                f"| {i} | {cb['name']} | "
                f'<span style="color:{pct_clr};font-weight:bold;">{cb["pct"]:+.2f}%</span> |'
            )
        lines.append("")
        lines.append(
            f'<div style="color:{C["text_muted"]};font-family:宋体;font-size:8pt;">'
            f'概念板块多为短期情绪驱动，需结合 Gate 评分综合判断持续性。</div>'
        )
        lines.append("")

    # 评分参考说明
    lines.append(
        f'<div style="background:{C["bg_section"]};'
        f'border:1px solid {C["border"]};'
        f'padding:10px 16px;margin:8px 0;border-radius:3px;">'
        f'<div style="color:{C["text_sec"]};font-family:宋体;font-size:9pt;">'
        f'<strong>评分参考</strong>：≥75 重点关注 ｜ ≥60 关注 ｜ ≥45 持有 ｜ ≥30 观望 ｜ &lt;30 回避'
        f'</div>'
        f'</div>'
    )
    lines.append("")

    # ═══════════════════ L3: 个股排名 ═══════════════════════
    pool_count = 0
    extra_count = 0
    if not top_stocks.empty:
        for _, r in top_stocks.iterrows():
            is_ind = r.get("is_independent", False) if "is_independent" in r.index else False
            is_pool = r.get("in_strong_pool", True) if "in_strong_pool" in r.index else True
            if is_ind or not is_pool:
                extra_count += 1
            else:
                pool_count += 1

    stock_insight = (
        f"强势行业池覆盖 {pool_count} 只标的，评分显著优于补位标的"
        if pool_count >= extra_count else
        f"补位标的数量较多（{extra_count}只），强势行业池有待扩容"
    )
    lines.append(
        T("h2",
          f'color:{C["brand_navy"]};font-family:宋体;font-size:14pt;'
          f'border-bottom:2px solid {C["brand_navy"]};padding-bottom:4px;'
          f'margin:20px 0 12px 0;',
          "三、个股评分排名 (L3)")
    )
    lines.append("")

    # L3 核心观点
    lines.append(
        f'<div style="background:{C["bg_section"]};'
        f'border:1px solid {C["border"]};'
        f'border-left:4px solid {C["brand_navy"]};'
        f'padding:12px 16px;margin:8px 0 16px 0;border-radius:3px;">'
        f'<div style="color:{C["text_sec"]};font-family:宋体;font-size:9pt;'
        f'margin-bottom:2px;">选股概况</div>'
        f'<div style="color:{C["text_dark"]};font-family:宋体;font-size:10.5pt;'
        f'font-weight:bold;">{stock_insight}。强势行业池内 {total_scored} 只股票参与评分。</div>'
        f'</div>'
    )
    lines.append("")

    lines.append(
        f'<div style="background:{C["red_bg"]};border-left:4px solid {C["red"]};'
        f'padding:8px 14px;margin:8px 0;border-radius:2px;">'
        f'<span style="color:{C["red"]};font-family:宋体;font-size:9pt;">'
        f'<strong>重要声明</strong>：以下标的仅表示模型评分排名，'
        f'<strong>不构成任何投资推荐</strong>。</span>'
        f'</div>'
    )
    lines.append("")

    if not top_stocks.empty:
        lines.append("| 排名 | 代码 | 名称 | 行业归属 | 类型 | 趋势 | Alpha | 量能 | 资金 | 综合分 | 形态 |")
        lines.append("|---:|:---|:---|:---|:---:|---:|---:|---:|---:|---:|---:|")

        pool_rows = []
        extra_rows = []
        for _, r in top_stocks.iterrows():
            is_ind = r.get("is_independent", False) if "is_independent" in r.index else False
            is_pool = r.get("in_strong_pool", True) if "in_strong_pool" in r.index else True
            industry = r.get("行业_display", r.get("行业", "-"))
            if industry == "综合":
                industry = "—"

            if is_ind:
                typ = f'<span style="color:{C["purple"]};font-family:宋体;font-size:10.5pt;font-weight:bold">独立</span>'
                extra_rows.append((r, industry, typ))
            elif not is_pool:
                typ = f'<span style="color:{C["amber"]};font-family:宋体;font-size:10.5pt;font-weight:bold">补位</span>'
                extra_rows.append((r, industry, typ))
            else:
                typ = f'<span style="color:{C["green"]};font-family:宋体;font-size:10.5pt;font-weight:bold">强池</span>'
                pool_rows.append((r, industry, typ))

        for i, (r, industry, typ) in enumerate(pool_rows, 1):
            pm = r.get("pattern_mult", 1.0)
            pm_label = f"×{pm:.2f}" if abs(pm - 1.0) > 0.01 else "—"
            lines.append(
                f"| {i} | `{r['symbol']}` | {r['name']} | {industry} | {typ} | "
                f"{r['趋势分']:.0f} | {r['Alpha分']:.0f} | {r['量能分']:.0f} | "
                f"{r['资金分']:.0f} | **{r['score']:.1f}** | {pm_label} |"
            )

        if extra_rows:
            start_rank = len(pool_rows) + 1
            for i, (r, industry, typ) in enumerate(extra_rows, start_rank):
                pm = r.get("pattern_mult", 1.0)
                pm_label = f"×{pm:.2f}" if abs(pm - 1.0) > 0.01 else "—"
                lines.append(
                    f"| {i} | `{r['symbol']}` | {r['name']} | {industry} | {typ} | "
                    f"{r['趋势分']:.0f} | {r['Alpha分']:.0f} | {r['量能分']:.0f} | "
                    f"{r['资金分']:.0f} | **{r['score']:.1f}** | {pm_label} |"
                )

        lines.append("")
        lines.append(
            f'<div style="color:{C["text_sec"]};font-family:宋体;font-size:9pt;">'
            f'强势行业池内 {total_scored} 只股票参与评分。"强池"=强势行业筛选，'
            f'"补位"=评分补充。形态=涨停突破+经典形态+缠论买点的乘性加成。'
            f'趋势(0-30) MACD+缠论，Alpha(0-25) 月涨幅+价格位置，'
            f'量能(0-25) 量价健康度，资金(0-20) 技术推断。</div>'
        )
    else:
        lines.append(
            f'<div style="color:{C["text_muted"]};font-family:宋体;'
            f'font-size:10.5pt;padding:8px 0;">本月暂无评分数据。</div>'
        )
    lines.append("")

    # ═══════════════════ RISK & DISCLAIMER ═══════════════════
    lines.append(
        f'<hr style="border:none;border-top:1px solid {C["border"]};'
        f'margin:24px 0 16px 0;">'
    )
    lines.append("")
    lines.append(
        T("h2",
          f'color:{C["brand_navy"]};font-family:宋体;font-size:13pt;'
          f'margin:16px 0 8px 0;',
          "四、风险提示与免责声明")
    )
    lines.append("")

    lines.append(
        f'<div style="background:{C["amber_bg"]};border-left:4px solid {C["amber"]};'
        f'padding:10px 16px;margin:8px 0;border-radius:2px;">'
        f'<span style="color:{C["amber"]};font-family:宋体;font-size:10pt;'
        f'font-weight:bold;">⚠ 重要风险提示</span>'
        f'</div>'
    )
    lines.append("")
    lines.append(
        f'<ol style="color:{C["text_body"]};font-family:宋体;font-size:9.5pt;'
        f'padding-left:20px;margin:4px 0 12px 0;">'
    )
    for risk_text in [
        "模型失效风险：量化模型基于历史数据统计规律构建，市场环境变化可能导致因子失效或模型表现偏离历史回测结果。",
        "数据风险：本报告数据来源于公开信息，不保证其完全准确、完整或及时。",
        "市场风险：证券投资存在本金损失风险，过往表现不代表未来收益。",
        "流动性风险：部分标的可能存在流动性不足风险，模型未充分考虑交易成本与冲击成本。",
        "政策风险：宏观政策、监管规则变化可能对模型结果产生重大影响。",
    ]:
        lines.append(f'<li style="margin-bottom:4px;">{risk_text}</li>')
    lines.append('</ol>')
    lines.append("")

    lines.append(
        f'<div style="background:{C["bg_section"]};'
        f'border-left:4px solid {C["text_muted"]};'
        f'padding:10px 16px;margin:8px 0;border-radius:2px;">'
        f'<span style="color:{C["text_dark"]};font-family:宋体;font-size:10pt;'
        f'font-weight:bold;">📋 免责声明</span>'
        f'</div>'
    )
    lines.append("")
    lines.append(
        f'<ol style="color:{C["text_body"]};font-family:宋体;font-size:9.5pt;'
        f'padding-left:20px;margin:4px 0 12px 0;">'
    )
    for disc_text in [
        "本报告由量化系统基于公开数据和量化模型自动生成，<strong>不构成任何投资建议、要约或承诺</strong>。",
        "报告中的任何观点、结论、模型输出均不代表对任何证券的投资价值作出评价或保证。",
        "投资者应根据自身财务状况、风险承受能力及投资目标，独立做出投资决策并自行承担全部投资风险。",
    ]:
        lines.append(f'<li style="margin-bottom:4px;">{disc_text}</li>')
    lines.append('</ol>')
    lines.append("")

    lines.append(
        f'<hr style="border:none;border-top:1px solid {C["border"]};'
        f'margin:16px 0 8px 0;">'
    )
    lines.append(
        f'<div style="color:{C["text_muted"]};font-family:宋体;font-size:8pt;'
        f'text-align:center;">报告生成于 {today_str} {datetime.now().strftime("%H:%M")} · 系统自动生成</div>'
    )
    lines.append("")

    return "\n".join(lines)


# ═══════════════════════ 主入口 ═══════════════════════

def main():
    logger.info("=== 量化日报 V3.0 L1→L2→L3 硬过滤管道 ===")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 加载数据（DataStore）
    store = load_all_from_cache(CACHE_DIR, n_stocks=0)
    logger.info(f"加载: {len(store.stock_names)} 只股票")

    stock_names = _load_stock_names()
    benchmark_close = get_benchmark(store)

    # 构建友好字典供下游函数使用
    _MARKET_NAMES = ["shanghai", "shenzhen", "chinext", "csi300"]
    _ETF_NAMES = ["证券", "银行", "军工", "芯片", "新能源车", "光伏",
                  "消费", "医药", "酒", "科技", "有色", "煤炭", "汽车", "半导体"]

    market_daily: dict[str, pd.DataFrame] = {}
    for name in _MARKET_NAMES:
        df = store.get_daily(name)
        if df is not None:
            market_daily[name] = df

    market_weekly: dict[str, pd.DataFrame] = {}
    for name in _MARKET_NAMES:
        df = store.get_weekly(name)
        if df is not None:
            market_weekly[name] = df

    etf_weekly: dict[str, pd.DataFrame] = {}
    for name in _ETF_NAMES:
        df = store.get_weekly(name)
        if df is not None:
            etf_weekly[name] = df

    stock_data: dict[str, pd.DataFrame] = {}
    for sym in store.stock_names:
        df = store.get_daily(sym)
        if df is not None:
            stock_data[sym] = df

    # ── 概念板块指数合并到 L2（与 backtest.runner 相同模式）──
    try:
        from backtest.runner import _build_concept_indices as _bci_concept
        symbol_etf_map = _load_etf_tags_map(stock_names)
        concept_indices = _bci_concept(store, list(store.stock_names), symbol_etf_map)
        if concept_indices:
            n_concepts = 0
            for cname, cdf in concept_indices.items():
                if cname in etf_weekly:
                    continue
                if len(cdf) < 26:
                    continue
                c_weekly = cdf.resample("W-FRI").agg({
                    "open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum",
                }).dropna()
                if len(c_weekly) < 4:
                    continue
                etf_weekly[cname] = c_weekly
                n_concepts += 1
            logger.info(f"L2 合并: {len(_ETF_NAMES)} ETF + {n_concepts} 概念板块")
    except Exception as e:
        logger.warning(f"概念板块指数构建失败（不影响主流程）: {e}")

    # ═══════════════════════ L1: 大盘环境 ═══════════════════════
    l1_result = None
    l1_market_state = "volatile"
    try:
        from gate.layer1_market import assess_market
        if market_weekly:
            l1_result = assess_market(market_weekly)
            l1_market_state_map = {"牛市": "bull", "震荡": "volatile", "偏弱": "weak", "熊市": "bear"}
            l1_market_state = l1_market_state_map.get(l1_result.market_state, "volatile")
            logger.info(f"L1: {l1_result.market_state} | 均分{l1_result.avg_score:.1f} | 仓位{l1_result.actual_position_pct*100:.0f}%")
    except Exception as e:
        logger.warning(f"L1 评估失败: {e}")

    # 市场指数行情
    market_indices = _get_market_indices(market_daily)
    logger.info(f"指数: {len(market_indices)} 个")

    # ═══════════════════════ L2: 行业 Gate 排名 ═══════════════════════
    strong_sectors = None
    sector_rankings = []
    cross_validation = []
    concept_rankings = []
    try:
        sector_rankings, strong_sectors = _get_sector_rankings_from_gate(
            etf_weekly, benchmark_close, l1_market_state,
        )
        logger.info(f"L2 Gate: {len(strong_sectors)} 个强势行业, {len(sector_rankings)} 候选")

        # 概念板块热力排名
        concept_rankings = _get_concept_board_rankings()
        logger.info(f"概念板块: {len(concept_rankings)} 个")

        # 申万行业辅助交叉验证
        cross_validation = _compute_cross_validation(
            etf_weekly, sector_rankings,
        )
    except Exception as e:
        logger.warning(f"L2 Gate 评估失败，使用降级方案: {e}")
        sector_rankings = _get_sector_rankings_fallback(etf_weekly)
        logger.info(f"行业降级: {len(sector_rankings)} 个")

    # ═══════════════════════ L3: 选股 ═══════════════════════
    l3_path = CACHE_DIR / "l3_scores_all.parquet"
    l3_scores = None
    top_stocks = pd.DataFrame()
    total_scored = 0

    # ── 检查 L3 缓存覆盖是否足够 ──
    if l3_path.exists():
        l3_scores = pd.read_parquet(l3_path)
        n_unique = l3_scores["symbol"].nunique()
        n_expected = len(stock_data)
        latest_in_file = l3_scores["month"].max()
        logger.info(f"L3 缓存: {n_unique}/{n_expected} 只股票, 最新 {latest_in_file}")

        # 覆盖不足时告警（不自动重算——耗时长，需手动运行 _batch_l3.py）
        if n_unique < max(200, n_expected * 0.8):
            logger.warning(f"L3 覆盖不足 ({n_unique}/{n_expected})，请手动运行: python scripts/_batch_l3.py")
            logger.warning("使用覆盖不全的缓存继续，选股范围受限")
        current_m = datetime.now().strftime("%Y-%m")
        if current_m > latest_in_file:
            logger.info(f"L3 最新月份 {latest_in_file}，当前 {current_m}，数据非最新")
    else:
        logger.warning(f"L3 缓存不存在，请先运行: python scripts/_batch_l3.py")

    # ── L3 选股 ──
    if l3_scores is not None and not l3_scores.empty:
        available = set(stock_data.keys())
        l3_scores = l3_scores[l3_scores["symbol"].isin(available)].copy()
        top_stocks, total_scored = _get_top_stocks(l3_scores, stock_names, stock_data,
                                                   strong_sectors=strong_sectors, top_n=15)

    if not top_stocks.empty:
        logger.info(f"L3 Top15 评分: {top_stocks['score'].iloc[0]:.2f} ~ {top_stocks['score'].iloc[-1]:.2f}")
        in_pool = top_stocks.get("in_strong_pool", pd.Series([True] * len(top_stocks)))
        independent = top_stocks.get("is_independent", pd.Series([False] * len(top_stocks)))
        logger.info(f"  强势池内: {in_pool.sum()} | 独立强势: {independent.sum()}")
    else:
        logger.warning("无 L3 评分数据")

    # ═══════════════════════ 生成日报 ═══════════════════════
    md = generate_markdown(
        market_indices=market_indices,
        sector_rankings=sector_rankings,
        top_stocks=top_stocks,
        today_str=today_str,
        total_scored=total_scored,
        l1_result=l1_result,
        cross_validation=cross_validation,
        strong_sectors=strong_sectors,
        concept_rankings=concept_rankings,
    )

    md_path = OBSIDIAN_DIR / f"{today_str}-量化日报.md"
    md_path.write_text(md, encoding="utf-8")
    logger.info(f"日报: {md_path}")

    # 控制台摘要
    print()
    if market_indices:
        top_idx = market_indices[0]
        print(f"  {top_idx['name']}: {top_idx['close']:.2f} ({top_idx['pct']:+.2%})")
    if l1_result:
        pos = l1_result.actual_position_pct * 100
        print(f"  L1: {l1_result.market_state} | 建议仓位 {pos:.0f}% | 均分 {l1_result.avg_score:.1f}")
    if not top_stocks.empty:
        print(f"  L3 Top1: {top_stocks['symbol'].iloc[0]} {top_stocks['name'].iloc[0]} ({top_stocks['score'].iloc[0]:.2f})")
        print(f"    行业: {top_stocks['行业_display'].iloc[0]}")
    if sector_rankings:
        print(f"  最强行业: {sector_rankings[0]['name']} (评分 {sector_rankings[0]['score']:.1f}) [{sector_rankings[0]['recommend']}]")
    if strong_sectors:
        print(f"  强势行业池: {', '.join(sorted(strong_sectors))}")
    print(f"  日报: {OBSIDIAN_DIR}")


def _compute_cross_validation(etf_weekly: dict, gate_rankings: list[dict]) -> list[dict]:
    """28个申万行业辅助交叉验证 — 对比L2 Gate评分与申万引擎排名"""
    try:
        from models.sector import SectorStrengthEngine
        engine = SectorStrengthEngine()
        engine.load_data()
        today = datetime.now().strftime("%Y-%m-%d")
        sw_strengths = engine.get_sector_strength_at(today)
        if sw_strengths is None or sw_strengths.empty:
            return []

        # 取 Top 5 申万行业
        sw_top = sw_strengths.nlargest(5)
        gate_names = {r["name"] for r in gate_rankings[:5]}

        results = []
        for sw_name, sw_score in sw_top.items():
            # 简单匹配: 如果申万行业名包含在Gate ETF名中
            match = any(sw_name[:2] in g for g in gate_names)
            results.append({
                "sw_name": sw_name,
                "sw_score": round(sw_score, 1),
                "converge": match,
            })
        return results
    except Exception as e:
        logger.debug(f"交叉验证跳过: {e}")
        return []


if __name__ == "__main__":
    main()
