"""统一日志模块

使用 SafeRotatingFileHandler（copy-truncate 模式）替代 RotatingFileHandler，
解决多进程共写同一日志文件时 rename 失败（WinError 32）的问题。
"""

import logging
import os
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

_FILE_HANDLER_ADDED = False


class SafeRotatingFileHandler(RotatingFileHandler):
    """安全轮转处理器 — copy-truncate 而非 rename。

    RotatingFileHandler 在 doRollover 时用 os.rename 将当前日志移到 .1。
    当有其他进程（serve_report / run_pipeline）持有 pipeline.log 的文件句柄时，
    Windows 下 rename 报 PermissionError: [WinError 32]。

    本类覆盖 doRollover，用 shutil.copy2 + open(w).truncate 替代 rename。
    代价：copy 与 truncate 之间其他进程写入的数行日志在截断中丢失（可接受）。
    """

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None

        try:
            # 轮转备份：copy2 + remove 替代 rename
            for i in range(self.backupCount - 1, 0, -1):
                sfn = self.rotation_filename(self.baseFilename + f".{i}")
                dfn = self.rotation_filename(self.baseFilename + f".{i + 1}")
                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    # rename 也可能失败（其他进程持有备份文件），用 copy+remove
                    try:
                        os.rename(sfn, dfn)
                    except OSError:
                        import shutil
                        shutil.copy2(sfn, dfn)
                        os.remove(sfn)

            # 主文件：copy(当前 → .1) + truncate(当前)
            if os.path.exists(self.baseFilename):
                dfn = self.rotation_filename(self.baseFilename + ".1")
                import shutil
                shutil.copy2(self.baseFilename, dfn)
                # truncate：即使其他进程持有句柄，以 "w" 模式打开也可截断
                with open(self.baseFilename, "w", encoding="utf-8") as f:
                    f.truncate(0)
        except OSError as e:
            # 轮转失败不阻断日志写入，静默吞掉
            pass

        self.stream = self._open()


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
        fh = SafeRotatingFileHandler(
            str(log_dir / "pipeline.log"),
            maxBytes=20 * 1024 * 1024,  # 20MB
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logging.getLogger().addHandler(fh)
        _FILE_HANDLER_ADDED = True

    return logger


def get_logger(name: str = "quant") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger
