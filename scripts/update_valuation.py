"""估值数据更新脚本 — python scripts/update_valuation.py

拉取腾讯全市场估值 → 校验门(6道) → 写入 stock_valuation → 记 update_log

由 run_pipeline.py 自动调用（步骤 ①.5），也可独立运行用于调试。
"""

import sys
from pathlib import Path

from datetime import datetime

import pandas as pd

from data.local.warehouse import LocalDataWarehouse
from data.sources.tencent_valuation import fetch_all_valuation
from core.logger import get_logger

logger = get_logger("scripts.update_valuation")

# ── 校验门阈值 ──
MIN_ROWS = 3000                # 总行数
MIN_MV_NONNULL = 2000          # 市值非空行数
MAX_MV_NULL_RATE = 0.30        # 市值缺失率上限
MAX_MV_ZERO_RATE = 0.20        # 市值 <= 0 比率上限
MAX_PE_NULL_RATE = 0.50        # PE 缺失率上限（A 股亏损公司 ~40%）
MAX_PE_EXTREME_RATE = 0.01     # PE 极端值率上限 (abs > 100000)


def validate_valuation(df: pd.DataFrame) -> bool:
    """6 道校验门，全部通过返回 True。

    任一不通过 → 输出详细统计 + 返回 False，不写入。
    """
    checks = [
        ("总行数", len(df) >= MIN_ROWS, f"{len(df)} >= {MIN_ROWS}"),
        ("市值非空行数", df["total_mv"].notna().sum() >= MIN_MV_NONNULL,
         f"{df['total_mv'].notna().sum()} >= {MIN_MV_NONNULL}"),
        ("市值缺失率", df["total_mv"].isna().sum() / len(df) < MAX_MV_NULL_RATE,
         f"{df['total_mv'].isna().sum() / len(df):.1%} < {MAX_MV_NULL_RATE:.0%}"),
        ("市值<=0比率", (df["total_mv"].fillna(0) <= 0).sum() / len(df) < MAX_MV_ZERO_RATE,
         f"{(df['total_mv'].fillna(0) <= 0).sum() / len(df):.1%} < {MAX_MV_ZERO_RATE:.0%}"),
        ("PE缺失率", df["pe_ttm"].isna().sum() / len(df) < MAX_PE_NULL_RATE,
         f"{df['pe_ttm'].isna().sum() / len(df):.1%} < {MAX_PE_NULL_RATE:.0%}"),
        ("PE极端值率", (df["pe_ttm"].fillna(0).abs() > 100000).sum() / len(df) < MAX_PE_EXTREME_RATE,
         f"{(df['pe_ttm'].fillna(0).abs() > 100000).sum() / len(df):.1%} < {MAX_PE_EXTREME_RATE:.0%}"),
    ]

    all_pass = True
    logger.info("估值校验门检查:")
    for name, passed, detail in checks:
        tag = "PASS" if passed else "FAIL"
        logger.info(f"  [{tag}] {name}: {detail}")
        if not passed:
            all_pass = False

    if not all_pass:
        logger.warning("校验门未通过，跳过估值写入")

    return all_pass


def update_valuation() -> bool:
    """主流程：拉取 → 校验（失败重抓一次） → 写入 → 记日志。返回是否成功。"""
    today = datetime.now().strftime("%Y-%m-%d")

    for attempt in range(2):
        logger.info(f"拉取全市场估值数据 ({today})..."
                    + (f" (第 {attempt + 1} 次)" if attempt > 0 else ""))
        df = fetch_all_valuation()
        if df.empty:
            logger.warning("估值数据为空，跳过")
            return False
        if validate_valuation(df):
            break
        if attempt == 0:
            logger.info("校验未通过，30s 后重试...")
            import time
            time.sleep(30)
    else:
        logger.warning("重试后校验仍未通过，跳过估值写入")
        return False

    # 写入
    df["date"] = today
    wh = LocalDataWarehouse()
    n = wh.update_valuation(df)

    # 记日志
    wh.mark_updated("stock_valuation", row_count=n)
    logger.info(f"估值数据更新完成: {n} 行, 日期 {today}")
    return True


def main():
    success = update_valuation()
    return 0 if success else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
