"""pytest 公共配置 — 所有测试自动注入项目根目录到 sys.path"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
