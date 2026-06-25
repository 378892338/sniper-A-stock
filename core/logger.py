"""统一日志模块"""

import logging
import sys
from pathlib import Path

_FILE_HANDLER_ADDED = False


def setup_logger(name: str = "quant", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    global _FILE_HANDLER_ADDED
    if not _FILE_HANDLER_ADDED:
        log_dir = Path(__file__).resolve().parent.parent / "outputs/reports"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_dir / "pipeline.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        logging.getLogger().addHandler(fh)
        _FILE_HANDLER_ADDED = True

    return logger


def get_logger(name: str = "quant") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger
