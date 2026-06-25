"""补全缺失股票日线数据（使用 Fetcher 反检测层）

串行执行，不依赖多进程（Windows spawn 下 multiprocessing 不可靠，
且 Fetcher 自带反检测延时，并发会触发限流）。

用法:
  python scripts/fix_missing_data.py                              # 补全所有缺失active股票
  python scripts/fix_missing_data.py --symbols 000001,000002      # 补全指定股票
  python scripts/fix_missing_data.py --start 2024-01-01 --end 2026-06-05
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import datetime

import pandas as pd

from data.local.warehouse import LocalDataWarehouse
from core.logger import get_logger

logger = get_logger("scripts.fix_missing_data")


def main():
    parser = argparse.ArgumentParser(description="补全缺失股票日线")
    parser.add_argument("--symbols", type=str, default="",
                        help="指定股票代码列表，逗号分隔（默认补所有 active 缺失的）")
    parser.add_argument("--start", type=str, default="2024-01-01",
                        help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="",
                        help="结束日期 YYYY-MM-DD（默认当天）")
    args = parser.parse_args()
    end = args.end or datetime.now().strftime("%Y-%m-%d")

    warehouse = LocalDataWarehouse()
    from shared.fetcher import Fetcher
    fetcher = Fetcher()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        logger.info(f"指定 {len(symbols)} 只股票")
    else:
        conn = warehouse._connect()
        try:
            have = set(r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM daily_bars").fetchall())
        finally:
            conn.close()
        stock_df = warehouse.get_stock_list(status="active")
        all_active = stock_df["symbol"].tolist() if not stock_df.empty else []
        symbols = [s for s in all_active if s not in have and not s.startswith("920")]
        logger.info(f"需补: {len(symbols)} 只（共 {len(all_active)} active, 已有 {len(have)}）")

    if not symbols:
        logger.info("全部已有数据，无需补全")
        return

    total_ok, total_fail = 0, 0
    all_dfs = []

    for i, sym in enumerate(symbols):
        logger.info(f"[{i+1}/{len(symbols)}] {sym}...")
        try:
            df = fetcher.fetch_stock_daily(sym, args.start, end)
            if df is not None and not df.empty:
                df["symbol"] = sym
                all_dfs.append(df)
                total_ok += 1
            else:
                total_fail += 1
                logger.warning(f"  FAIL: 空数据")
        except Exception as e:
            total_fail += 1
            logger.warning(f"  FAIL: {e}")

        # 每 5 只入库一次，避免内存堆积
        if all_dfs and (len(all_dfs) >= 5 or i == len(symbols) - 1):
            # normalize 已返回 date 作为列，直接 concat
            combined = pd.concat(all_dfs, ignore_index=False)
            combined = combined.reset_index(drop=True)
            warehouse.store_daily_bars(combined, if_exists="append")
            logger.info(f"  入库: {combined['symbol'].nunique()} 只, {len(combined)} 行")
            all_dfs = []

    logger.info(f"完成: OK={total_ok} FAIL={total_fail} / {len(symbols)}")


if __name__ == "__main__":
    main()
