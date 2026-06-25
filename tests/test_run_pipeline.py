"""全链路自动化测试 — 验证 run_pipeline.py"""

import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_pipeline.py"


class TestRunPipelineImport:

    def test_file_exists(self):
        assert SCRIPT_PATH.exists(), f"文件不存在: {SCRIPT_PATH}"

    def test_file_parses(self):
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        compile(source, str(SCRIPT_PATH), "exec")

    def test_importable(self):
        spec = importlib.util.spec_from_file_location("run_pipeline", SCRIPT_PATH)
        assert spec is not None

    def test_contains_run_pipeline(self):
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "def run_pipeline(" in source

    def test_contains_catch_up_logic(self):
        """应包含开机补跑相关函数。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "_get_missing_dates" in source or "补跑" in source

    def test_obsidian_path_configured(self):
        """应配置 Obsidian 输出路径。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "Obsidian" in source

    def test_references_daily_report(self):
        """应引用 daily_report。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "daily_report" in source

    def test_references_run_live(self):
        """应引用 run_live。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        assert "run_live" in source
