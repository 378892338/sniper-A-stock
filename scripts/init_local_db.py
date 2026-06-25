"""初始化本地数据仓库 — 全量下载历史数据到 SQLite。

运行方式: python scripts/init_local_db.py [--start 2000-01-01] [--no-stocks]
"""

import sys
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.local import LocalDataWarehouse
from data.local.updater import update_all
from core.logger import get_logger

logger = get_logger("scripts.init_local_db")


def main():
    parser = argparse.ArgumentParser(description="初始化本地数据仓库")
    parser.add_argument("--start", default="2000-01-01", help="起始日期 (默认 2000-01-01)")
    parser.add_argument("--no-stocks", action="store_true", help="跳过个股日线（只下载指数）")
    args = parser.parse_args()

    warehouse = LocalDataWarehouse()
    logger.info(f"数据仓库路径: {warehouse.db_path}")

    update_all(
        warehouse,
        start=args.start,
        include_daily_bars=not args.no_stocks,
    )

    logger.info("初始化完成！")
    stats = warehouse.table_stats()
    for tbl, cnt in stats.items():
        print(f"  {tbl}: {cnt} 行")


if __name__ == "__main__":
    main()
