"""一次性修复：补全所有股票日线数据

问题:
  - 仅 2,557/5,515 只股票有日线数据（主板 1,787/4,117 + 创业板 770/1,398）
  - 大量股票因数据缺失无法进入 L2 评分 → 日报候选

流程:
  1. 全量更新股票列表（补齐所有缺失股票）
  2. 找出 daily_bars 中没有数据的全部股票（不限板块）
  3. 用 updater.update_daily_bars_all() 补下载
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.local.warehouse import LocalDataWarehouse
from data.local import updater
from core.logger import get_logger

logger = get_logger("scripts.fix_cyb_data")


def main():
    warehouse = LocalDataWarehouse()

    # ── 1. 更新股票列表（补齐缺失股票） ──
    logger.info("=" * 50)
    logger.info("第一步: 全量更新股票列表")
    logger.info("=" * 50)
    updater.update_stock_list(warehouse)

    # ── 2. 找出所有缺失数据的股票 ──
    logger.info("=" * 50)
    logger.info("第二步: 找出所有缺失数据的股票")
    logger.info("=" * 50)
    stock_df = warehouse.get_stock_list(status="active")
    all_stocks = stock_df["symbol"].tolist()
    logger.info(f"活跃股票总数: {len(all_stocks)}")

    # 已有数据的
    have_data = set()
    import sqlite3
    conn = warehouse._connect()
    try:
        rows = conn.execute("SELECT DISTINCT symbol FROM daily_bars").fetchall()
        have_data = {r[0] for r in rows}
    finally:
        conn.close()
    logger.info(f"已有日线数据: {len(have_data)} 只")

    missing = [s for s in all_stocks if s not in have_data]
    logger.info(f"需补下载: {len(missing)} 只")

    # 按板块统计
    from collections import Counter
    board_cnt = Counter()
    for s in missing:
        if s.startswith("30"):
            board_cnt["创业板"] += 1
        elif s.startswith("68"):
            board_cnt["科创板"] += 1
        elif s.startswith("8"):
            board_cnt["北交所"] += 1
        elif s.startswith(("00", "60")):
            board_cnt["主板"] += 1
        else:
            board_cnt["其他"] += 1
    for board, cnt in board_cnt.most_common():
        logger.info(f"  {board}: {cnt} 只")

    if not missing:
        logger.info("✅ 全部已有数据，无需补下载")
        return

    # ── 3. 补下载 ──
    logger.info("=" * 50)
    logger.info("第三步: 补下载缺失数据（Baostock 20进程并发）")
    logger.info("    预计耗时: 10~20 分钟")
    logger.info("=" * 50)
    updater.update_daily_bars_all(
        warehouse,
        symbols=missing,
        start="2019-01-01",
        end=None,  # 到今天
        batch_size=200,
        max_workers=20,
    )

    # ── 4. 验证 ──
    logger.info("=" * 50)
    logger.info("第四步: 验证")
    logger.info("=" * 50)
    conn = warehouse._connect()
    try:
        after_cnt = conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily_bars").fetchone()[0]
    finally:
        conn.close()

    logger.info(f"修复后: {after_cnt}/{len(all_stocks)} 只有数据")
    if after_cnt >= len(all_stocks) * 0.9:
        logger.info(f"✅ 修复完成（{after_cnt}/{len(all_stocks)}）")
    else:
        logger.warning(f"⚠️ 仍有 {len(all_stocks) - after_cnt} 只缺失（可能部分 Baostock 无数据）")


if __name__ == "__main__":
    main()

