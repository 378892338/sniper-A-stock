"""统一路径管理 — 所有路径走 QUANT_DATA_ROOT 配置

约束（来自 quant-system-dev-pitfalls 坑 14）：
- 严禁硬编码本地绝对路径
- 任何路径都从 QUANT_DATA_ROOT 推导
- 用户特定路径走环境变量

典型用法：
    from config.paths import DATA_RAW_DIR, OUTPUT_DIR, META_DB_PATH
    df.to_parquet(DATA_RAW_DIR / "cache.parquet")
"""
import os
import sys
from pathlib import Path

# 数据仓根 — 用户必须设置 QUANT_DATA_ROOT
QUANT_DATA_ROOT = Path(os.environ.get("QUANT_DATA_ROOT", "D:/projects/quant-data")).resolve()

# 代码仓根 — 推导（不依赖 CWD）
_CODE_ROOT = Path(__file__).resolve().parents[1]
if str(_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT))

# ── 数据仓子目录 ──
DATA_LOCAL_DIR = QUANT_DATA_ROOT / "local"          # db 文件
DATA_RAW_DIR = QUANT_DATA_ROOT / "raw"              # 行情缓存
DATA_CACHE_DIR = QUANT_DATA_ROOT / "cache"          # 通用缓存
OUTPUT_DIR = QUANT_DATA_ROOT / "outputs"            # 结果/日志/调优
LOG_DIR = QUANT_DATA_ROOT / "logs"                  # 顶层日志
EXTERNAL_DATA_DIR = QUANT_DATA_ROOT / "external"    # 第三方数据（JQuant 等）

# 历史兼容：JQData 默认位置
META_DB_PATH = os.environ.get("META_DB_PATH", str(QUANT_DATA_ROOT / "local" / "meta.db"))

# 代码仓辅助路径（少数真源码需要写到代码仓下的目录）
DOCS_DIR = _CODE_ROOT / "docs"
SCRIPTS_DIR = _CODE_ROOT / "scripts"

# 确保数据仓目录存在
for _d in (DATA_LOCAL_DIR, DATA_RAW_DIR, DATA_CACHE_DIR, OUTPUT_DIR, LOG_DIR, EXTERNAL_DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def quant_path(*parts: str) -> Path:
    """拼接 QUANT_DATA_ROOT 下的相对路径。

    >>> quant_path("local", "meta.db")
    PosixPath('D:/projects/quant-data/local/meta.db')
    """
    return QUANT_DATA_ROOT.joinpath(*parts)


__all__ = [
    "QUANT_DATA_ROOT",
    "DATA_LOCAL_DIR", "DATA_RAW_DIR", "DATA_CACHE_DIR",
    "OUTPUT_DIR", "LOG_DIR", "EXTERNAL_DATA_DIR",
    "META_DB_PATH",
    "DOCS_DIR", "SCRIPTS_DIR",
    "quant_path",
]
