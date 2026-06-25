"""每周数据更新 — 增量更新本地 SQLite 数据仓库。

可配置为定时任务（Windows 计划任务 / crontab）每周运行一次。
每张表独立追踪更新时间，避免遗漏。
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.local import LocalDataWarehouse
from data.local.updater import (
    update_stock_list,
    update_trade_calendar,
    update_market_indices,
    update_sw_indices,
    update_daily_bars_all,
)
from core.logger import get_logger

logger = get_logger("scripts.weekly_update")

# 每张表默认往前追溯的天数（确保无遗漏）
LOOKBACK_DAYS = 7


def _calculate_start(warehouse: LocalDataWarehouse, table: str) -> str:
    """根据表的上次更新时间推算增量起始日期。"""
    last = warehouse.get_last_update(table)
    if last:
        last_dt = datetime.fromisoformat(last)
        return (last_dt - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    return "2000-01-01"


def main():
    warehouse = LocalDataWarehouse()
    today = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"=== 周度增量更新: {today} ===")

    # 每张表独立计算起始日期
    start_stock = _calculate_start(warehouse, "stock_list")
    start_cal = _calculate_start(warehouse, "trade_calendar")
    start_idx = _calculate_start(warehouse, "index_daily")
    start_sw = _calculate_start(warehouse, "sw_index_daily")
    start_bar = _calculate_start(warehouse, "daily_bars")

    # 1. 股票列表（全量替换，成本低）
    update_stock_list(warehouse)

    # 2. 交易日历（全量替换）
    update_trade_calendar(warehouse)

    # 3. 指数数据
    update_market_indices(warehouse, start=start_idx, end=today)

    # 4. 申万行业指数
    update_sw_indices(warehouse, start=start_sw, end=today)

    # 5. 个股日线（增量，最耗时）
    update_daily_bars_all(warehouse, start=start_bar, end=today)

    # 6. 申万行业成分股映射（预抓 SW2 缓存，保证 pipeline 启动时直接命中）
    logger.info("  预抓 SW2 成分股映射...")
    try:
        from data.industry import fetch_sw2_members
        sw2 = fetch_sw2_members()
        if sw2.empty:
            logger.warning("  SW2 成分股预抓失败（不影响后续）")
        else:
            logger.info(f"  SW2 成分股预抓成功: {len(sw2)} 条, {sw2['industry_l2'].nunique()} 行业")
    except Exception as e:
        logger.warning(f"  SW2 成分股预抓异常: {e}")

    stats = warehouse.table_stats()
    logger.info(f"=== 周度更新完成 ===")
    for tbl, cnt in stats.items():
        logger.info(f"  {tbl}: {cnt} 行")


if __name__ == "__main__":
    main()
