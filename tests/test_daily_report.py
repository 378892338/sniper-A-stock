"""日报生成测试 — 验证 daily_report.py"""

import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "daily_report.py"


class TestDailyReportImport:

    def test_file_exists(self):
        """daily_report.py 文件应存在。"""
        assert SCRIPT_PATH.exists(), f"文件不存在: {SCRIPT_PATH}"

    def test_file_parses(self):
        """daily_report.py 语法应正确。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        compile(source, str(SCRIPT_PATH), "exec")

    def test_importable(self):
        """daily_report.py 应可 import。"""
        spec = importlib.util.spec_from_file_location("daily_report", SCRIPT_PATH)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        assert mod is not None

    def test_contains_generate_report(self):
        """应包含 generate_report() 函数。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "def generate_report(" in source, "应包含 generate_report()"

    def test_contains_all_sections(self):
        """日报应包含所有节函数。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        sections = ["section_market", "section_sectors", "section_stocks",
                     "section_entry_filter", "section_portfolio",
                     "section_attribution", "section_config"]
        for s in sections:
            assert f"def {s}(" in source, f"缺少 {s}()"

    def test_contains_section_market(self):
        """日报第一节应有「市场状态」标题。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "市场状态" in source

    def test_contains_section_sectors(self):
        """日报第二节应有「强势板块」标题。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "强势板块" in source

    def test_contains_section_stocks(self):
        """日报第三节应有「候选个股」标题。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "候选个股" in source

    def test_contains_section_entry(self):
        """日报第四节应有「入场过滤」标题。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "入场过滤" in source

    def test_contains_section_portfolio(self):
        """日报第五节应有「持仓状态」标题。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "持仓状态" in source

    def test_contains_section_attribution(self):
        """日报第六节应有「打字机归因」标题。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "打字机归因" in source

    def test_contains_section_config(self):
        """日报第七节应有「当前配置参数」标题。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "当前配置参数" in source


class TestDailyReportOutput:

    def test_generate_report_returns_string(self):
        """generate_report() 应返回字符串。"""
        from scripts.daily_report import generate_report
        report = generate_report("2026-06-03")
        assert isinstance(report, str)
        assert len(report) > 100

    def test_report_contains_all_sections(self):
        """日报输出应包含所有 7 节。"""
        from scripts.daily_report import generate_report
        report = generate_report("2026-06-03")
        for title in ["市场状态", "强势板块", "候选个股",
                       "入场过滤", "持仓状态", "打字机归因",
                       "当前配置参数"]:
            assert title in report, f"缺少节: {title}"

    def test_report_date_in_title(self):
        """日报标题应包含日期。"""
        from scripts.daily_report import generate_report
        report = generate_report("2026-06-03")
        assert "2026-06-03" in report
