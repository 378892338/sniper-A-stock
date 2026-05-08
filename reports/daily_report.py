"""每日PDF报告生成

输出格式：
  - 分类指数强势 Top5
  - 每个分类指数下个股 Top3
  - 评分明细 + 信号标注
"""

import os
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from config.settings import OUTPUT_DIR

REPORT_DIR = OUTPUT_DIR / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


class DailyReportPDF(FPDF):
    """A4中文PDF日报"""

    def __init__(self, date_str: str):
        super().__init__(orientation='P', unit='mm', format='A4')
        self.date_str = date_str
        # 注册中文字体
        self._setup_font()
        self.set_auto_page_break(auto=True, margin=15)

    def _setup_font(self):
        """尝试加载系统中文字体"""
        font_paths = os.getenv("CN_FONT_PATH", "").split(";") if os.getenv("CN_FONT_PATH") else []
        font_paths += [
            "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑
            "C:/Windows/Fonts/simsun.ttc",   # 宋体
            "C:/Windows/Fonts/simhei.ttf",   # 黑体
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                self.add_font("CN", "", fp, uni=True)
                self.add_font("CN", "B", fp, uni=True)
                return
        # 找不到中文字体，用内置（会乱码）
        print("[WARN] 未找到中文字体，PDF中文可能乱码")

    def header(self):
        self.set_font("CN", "B", 16)
        self.cell(0, 10, f"量化因子日报 — {self.date_str}", new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font("CN", "", 9)
        self.cell(0, 5, f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x="LMARGIN", new_y="NEXT", align="C")
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("CN", "", 8)
        self.cell(0, 10, f"第{self.page_no()}页 — Quant System 自动生成", align="C")

    def _safe_text(self, text: str) -> str:
        """安全编码文本"""
        return str(text)

    def add_sector_strength_section(self, top_sectors: list):
        """分类指数强势Top5"""
        self.set_font("CN", "B", 14)
        self.cell(0, 8, "一、分类指数强弱排名 Top5", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        col_w = [50, 30, 30, 40, 40]
        headers = ["指数名称", "代码", "评分", "趋势", "动量"]
        self.set_font("CN", "B", 9)
        for h, w in zip(headers, col_w):
            self.cell(w, 6, h, border=1, align="C")
        self.ln()

        self.set_font("CN", "", 9)
        for i, (code, name, score) in enumerate(top_sectors):
            rank_label = f"#{i+1} {name}"
            self.cell(col_w[0], 6, rank_label, border=1)
            self.cell(col_w[1], 6, code, border=1, align="C")
            self.cell(col_w[2], 6, f"{score:.1f}", border=1, align="C")

            # 趋势标签
            if score >= 70:
                trend = "强势"
            elif score >= 50:
                trend = "中性"
            else:
                trend = "弱势"
            self.cell(col_w[3], 6, trend, border=1, align="C")

            # 动量
            if score >= 75:
                mom = "加速"
            elif score >= 50:
                mom = "稳定"
            else:
                mom = "减速"
            self.cell(col_w[4], 6, mom, border=1, align="C")
            self.ln()
        self.ln(4)

    def add_sector_stocks_section(self, sector_top3: dict, top_sectors: list):
        """每个强势分类指数下的个股Top3"""
        self.set_font("CN", "B", 14)
        self.cell(0, 8, "二、强势指数旗下个股 Top3", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        # 按强势排序展示
        ranked_codes = [code for code, _, _ in top_sectors]

        for code in ranked_codes:
            if code not in sector_top3 or not sector_top3[code]['stocks']:
                continue

            info = sector_top3[code]
            self.set_font("CN", "B", 11)
            self.set_fill_color(240, 240, 240)
            self.cell(0, 7, f"  {info['name']} ({code})", fill=True, new_x="LMARGIN", new_y="NEXT")
            self.ln(1)

            # 表头
            col_w = [25, 35, 35, 35, 35]
            headers = ["排名", "股票代码", "综合评分", "因子评分", "指数加成"]
            self.set_font("CN", "B", 8)
            for h, w in zip(headers, col_w):
                self.cell(w, 5, h, border=1, align="C")
            self.ln()

            self.set_font("CN", "", 8)
            for rank, (sym, s) in enumerate(info['stocks']):
                self.cell(col_w[0], 5, f"#{rank+1}", border=1, align="C")
                self.cell(col_w[1], 5, sym, border=1, align="C")
                self.cell(col_w[2], 5, f"{s['final_score']:.1f}", border=1, align="C")
                self.cell(col_w[3], 5, f"{s['factor_score']:.1f}", border=1, align="C")
                self.cell(col_w[4], 5, f"{s['sector_boost']:+.1f}", border=1, align="C")
                self.ln()
            self.ln(3)

    def add_signal_summary(self, all_scores: dict):
        """信号统计摘要"""
        self.set_font("CN", "B", 14)
        self.cell(0, 8, "三、评分分布统计", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        if not all_scores:
            return

        scores = [s['final_score'] for s in all_scores.values()]
        factor_scores = [s['factor_score'] for s in all_scores.values()]
        boosts = [s['sector_boost'] for s in all_scores.values()]

        import numpy as np

        self.set_font("CN", "", 9)
        stats = [
            ("有效股票数", f"{len(scores)}"),
            ("综合评分均值", f"{np.mean(scores):.1f}"),
            ("综合评分中位数", f"{np.median(scores):.1f}"),
            ("因子评分均值", f"{np.mean(factor_scores):.1f}"),
            ("指数加成均值", f"{np.mean(boosts):+.1f}"),
            (">70分股票数", f"{sum(1 for s in scores if s > 70)}"),
            (">50分股票数", f"{sum(1 for s in scores if s > 50)}"),
        ]
        for label, val in stats:
            self.cell(60, 5, f"  {label}: {val}", new_x="LMARGIN", new_y="NEXT")

    def add_disclaimer(self):
        """免责声明"""
        self.ln(5)
        self.set_font("CN", "", 7)
        self.set_text_color(128, 128, 128)
        self.multi_cell(0, 4,
            "免责声明：本报告由量化系统自动生成，仅供参考，不构成任何投资建议。"
            "因子评分基于历史数据回测，不代表未来表现。投资有风险，入市需谨慎。"
        )


def generate_daily_report(
    date_str: str,
    report_data: dict,
    output_dir: Path = None,
) -> Path:
    """生成每日PDF报告，返回文件路径"""
    if output_dir is None:
        output_dir = REPORT_DIR

    pdf = DailyReportPDF(date_str)

    # 首页
    pdf.add_page()
    pdf.add_sector_strength_section(report_data.get('top_sectors', []))
    pdf.add_sector_stocks_section(
        report_data.get('sector_top3', {}),
        report_data.get('top_sectors', []),
    )
    pdf.add_signal_summary(report_data.get('all_scores', {}))

    # 第二页：所有股票详细评分
    pdf.add_page()
    pdf.set_font("CN", "B", 14)
    pdf.cell(0, 8, "四、全部个股评分明细", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    all_scores = report_data.get('all_scores', {})
    if all_scores:
        sorted_stocks = sorted(all_scores.items(), key=lambda x: x[1]['final_score'], reverse=True)

        col_w = [25, 35, 30, 35, 35]
        headers = ["排名", "股票代码", "综合评分", "因子评分", "指数加成"]
        pdf.set_font("CN", "B", 8)
        for h, w in zip(headers, col_w):
            pdf.cell(w, 5, h, border=1, align="C")
        pdf.ln()

        pdf.set_font("CN", "", 8)
        for rank, (sym, info) in enumerate(sorted_stocks):
            if rank > 0 and rank % 35 == 0:
                pdf.add_page()
                pdf.set_font("CN", "B", 8)
                for h, w in zip(headers, col_w):
                    pdf.cell(w, 5, h, border=1, align="C")
                pdf.ln()
                pdf.set_font("CN", "", 8)

            pdf.cell(col_w[0], 4, f"#{rank+1}", border=1, align="C")
            pdf.cell(col_w[1], 4, sym, border=1, align="C")
            pdf.cell(col_w[2], 4, f"{info['final_score']:.1f}", border=1, align="C")
            pdf.cell(col_w[3], 4, f"{info['factor_score']:.1f}", border=1, align="C")
            boost = info.get('sector_boost', 0)
            pdf.cell(col_w[4], 4, f"{boost:+.1f}", border=1, align="C")
            pdf.ln()

    pdf.add_disclaimer()

    # 保存
    filename = f"quant_daily_{date_str.replace('-', '')}.pdf"
    path = output_dir / filename
    pdf.output(str(path))
    return path
