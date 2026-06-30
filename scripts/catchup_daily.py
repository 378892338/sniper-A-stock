"""快速补更今日日线 — 用 akshare_daily（新浪源）直连批量拉取

akshare_daily 速率: ~2s/只，10 worker = ~300 只/分钟
剩余 ~4200 只 → 预计 ~14 分钟
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from concurrent.futures import ThreadPoolExecutor, as_completed
from data.local.warehouse import LocalDataWarehouse
from data.sources.akshare import AkshareDailySource
from core.logger import get_logger

logger = get_logger("scripts.catchup")
today = "2026-06-26"
N_WORKERS = 10

wh = LocalDataWarehouse()

# 获取需更新的股票
stock_df = wh.get_stock_list(status="active")
all_symbols = [s for s in stock_df["symbol"].tolist() if not s.startswith("920")]

# 筛选缺失的
need = []
for sym in all_symbols:
    last = wh.get_last_date("daily_bars", "symbol", sym)
    if not last or last < today:
        need.append(sym)

logger.info(f"需补更: {len(need)}/{len(all_symbols)} 只 ({N_WORKERS} workers)")

ok = fail = 0
t0 = time.time()

def fetch_one(sym):
    try:
        ds = AkshareDailySource()
        df = ds.fetch_daily(sym, today, today)
        if df is not None and not df.empty:
            if "symbol" not in df.columns:
                df = df.assign(symbol=sym)
            # 过滤不在 daily_bars schema 中的列
            allowed = {"date","symbol","open","high","low","close","volume","amount","turnover","pct_chg"}
            extra = set(df.columns) - allowed
            if extra:
                df = df.drop(columns=list(extra))
            return sym, df
    except Exception as e:
        logger.debug(f"{sym}: {e}")
    return sym, None

with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
    futures = {pool.submit(fetch_one, sym): sym for sym in need}
    for i, future in enumerate(as_completed(futures)):
        sym = futures[future]
        try:
            sym2, df = future.result()
            if df is not None:
                wh.store_daily_bars(df, if_exists="append")
                ok += 1
            else:
                fail += 1
        except Exception as e:
            logger.debug(f"{sym}: {e}")
            fail += 1

        if (i + 1) % 200 == 0 or (i + 1) == len(need):
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (len(need) - i - 1) / rate if rate > 0 else 0
            logger.info(f"进度 {i+1}/{len(need)} ok={ok} fail={fail} "
                       f"速率={rate:.1f}/s ETA={eta:.0f}s")

# 最终统计
conn = wh._connect()
n = conn.execute(f'SELECT COUNT(DISTINCT symbol) FROM daily_bars WHERE date=\"{today}\"').fetchone()[0]
conn.close()
logger.info(f"完成: daily_bars {today} = {n} 只, 耗时 {time.time()-t0:.0f}s")
