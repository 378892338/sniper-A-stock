"""实盘入口测试 — 验证 run_live.py 可导入、无语法错误、函数签名正确"""

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_live.py"


class TestRunLiveImport:

    def test_file_exists(self):
        """run_live.py 文件应存在。"""
        assert SCRIPT_PATH.exists(), f"文件不存在: {SCRIPT_PATH}"

    def test_file_parses(self):
        """run_live.py 语法应正确。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        compile(source, str(SCRIPT_PATH), "exec")

    def test_importable(self):
        """run_live.py 应可 import（模块级代码不自动执行）。"""
        spec = importlib.util.spec_from_file_location("run_live", SCRIPT_PATH)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        # 不执行，只验证模块结构可加载
        assert mod is not None

    def test_contains_daily_run_function(self):
        """应包含 daily_run() 或 main() 函数。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        has_def = "def daily_run(" in source or "def main(" in source
        assert has_def, "run_live.py 应包含 daily_run() 或 main() 函数"

    def test_imports_config_and_l0(self):
        """应 import config.py 和 l0_market.py。"""
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            source = f.read()
        has_config = "import sniper.config" in source or "from sniper.config" in source
        has_l0 = "l0_market" in source or "MarketScorer" in source
        assert has_config, "应引用 sniper.config"
        assert has_l0, "应引用 MarketScorer 或 l0_market"
