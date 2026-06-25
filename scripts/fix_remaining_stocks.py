"""用 AKShare EM 端点补下载 Baostock 不覆盖的股票数据（含 volume）。

用法: python scripts/fix_remaining_stocks.py
"""

import sys, time, concurrent.futures
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import akshare as ak

from data.local.warehouse import LocalDataWarehouse
from core.logger import get_logger

logger = get_logger("scripts.fix_remaining_stocks")

MAX_WORKERS = 8
FETCH_START = "2019-01-01"
FETCH_END = "2026-06-06"


def _to_em_code(symbol: str) -> str:
    return f"sh{symbol}" if symbol.startswith(("6", "9")) else f"sz{symbol}"


def fetch_one(symbol: str) -> pd.DataFrame:
    """用 EM 端点获取一只股票的全部日线（含 volume）。"""
    code = _to_em_code(symbol)
    start = FETCH_START.replace("-", "")
    end = FETCH_END.replace("-", "")
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start, end_date=end,
        )
        if df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount",
        })
        df["date"] = pd.to_datetime(df["date"])
        df["symbol"] = symbol
        return df[["symbol", "date", "open", "high", "low", "close", "volume", "amount"]]
    except Exception as e:
        logger.debug(f"{symbol} EM 获取失败: {e}")
        return pd.DataFrame()


def main():
    wh = LocalDataWarehouse()

    conn = wh._connect()
    missing = [r[0] for r in conn.execute("""
        SELECT symbol FROM stock_list WHERE status='active'
        AND symbol NOT IN (SELECT DISTINCT symbol FROM daily_bars)
        AND symbol NOT LIKE '920%'
    """).fetchall()]
    conn.close()

    logger.info(f"需补下载: {len(missing)} 只股票 (EM 端点)")

    total_ok, total_rows = 0, 0
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut_map = {pool.submit(fetch_one, sym): sym for sym in missing}
        for i, fut in enumerate(concurrent.futures.as_completed(fut_map)):
            sym = fut_map[fut]
            try:
                df = fut.result()
                if df.empty:
                    continue
                wh.store_daily_bars(df, if_exists="append")
                total_ok += 1
                total_rows += len(df)
            except Exception as e:
                logger.warning(f"{sym} 写入失败: {e}")
            # 防反爬延迟
            time.sleep(0.3)
            if (i + 1) % 100 == 0:
                logger.info(f"  进度: {i+1}/{len(missing)}, {total_ok} ok, {total_rows} 行")

    elapsed = time.time() - t0
    logger.info(f"完成: {total_ok}/{len(missing)} ok, {total_rows} 行, 耗时 {elapsed/60:.1f} 分钟")

    # 最终验证
    conn = wh._connect()
    total = conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily_bars").fetchone()[0]
    vol_bad = conn.execute(
        "SELECT COUNT(*) FROM daily_bars WHERE date >= '2019-01-01' AND (volume IS NULL OR volume = 0)"
    ).fetchone()[0]
    conn.close()
    logger.info(f"最终: {total} 只股票, 2019+ volume缺失 {vol_bad} 行")


if __name__ == "__main__":
    main()
