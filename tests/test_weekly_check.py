"""周末归因监控测试 — 验证 weekly_check.py 可导入、语法正确"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "weekly_check.py"


class TestWeeklyCheckImport:

    def test_file_exists(self):
        assert SCRIPT_PATH.exists(), f"文件不存在: {SCRIPT_PATH}"

    def test_file_parses(self):
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        compile(source, str(SCRIPT_PATH), "exec")

    def test_contains_check_function(self):
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        has_check = "def weekend_check(" in source or "def run(" in source
        assert has_check, "应包含 weekend_check() 或 run() 函数"
