"""初始化数据库 + 下载全部数据"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from sniper.signals.store import SignalStore
from sniper.signals.download_all import download_all_signals
from core.logger import get_logger

logger = get_logger("sniper.scripts.init_db")


def main():
    logger.info("=" * 50)
    logger.info("初始化信号数据库 + 下载全部信号数据")
    logger.info("=" * 50)

    store = SignalStore()
    logger.info("信号表 schema 初始化完成")

    stats_before = store.table_stats()
    logger.info(f"下载前数据量: {stats_before}")

    results = download_all_signals(store)

    stats_after = store.table_stats()
    logger.info(f"下载后数据量: {stats_after}")

    logger.info("=" * 50)
    logger.info("下载汇总:")
    for name, rows in results.items():
        status = f"{rows} 行" if rows >= 0 else "失败"
        logger.info(f"  {name}: {status}")

    total = sum(v for v in results.values() if v > 0)
    logger.info(f"总计写入: {total} 行")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
