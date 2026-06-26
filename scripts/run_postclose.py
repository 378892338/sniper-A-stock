"""15:45 盘后完整链路：数据补更 → 日报生成

替代旧的 16:00 QuantDailyReport 独立任务。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from data.local.warehouse import LocalDataWarehouse
from data.local.updater import update_market_indices, update_sw_indices
from data.sources.akshare import AkshareDailySource
from core.logger import get_logger

logger = get_logger("scripts.postclose")
today = datetime.now().strftime("%Y-%m-%d")

wh = LocalDataWarehouse()

# 1. 指数更新
logger.info("=== 指数 ===")
update_market_indices(wh, start=today, end=today)
update_sw_indices(wh, start=today, end=today)

# 2. 个股日线补更
logger.info("=== 日线补更 ===")
stock_df = wh.get_stock_list(status="active")
symbols = [s for s in stock_df["symbol"].tolist() if not s.startswith("920")]
need = [s for s in symbols if not wh.get_last_date("daily_bars", "symbol", s) or wh.get_last_date("daily_bars", "symbol", s) < today]
logger.info(f"需补 {len(need)} 只")

ok = fail = 0
def _fetch(sym):
    try:
        ds = AkshareDailySource()
        df = ds.fetch_daily(sym, today, today)
        if df is not None and not df.empty:
            allowed = {"date","symbol","open","high","low","close","volume","amount","turnover","pct_chg"}
            extra = set(df.columns) - allowed
            if extra:
                df = df.drop(columns=list(extra))
            return sym, df
    except Exception:
        pass
    return sym, None

with ThreadPoolExecutor(max_workers=10) as pool:
    futures = {pool.submit(_fetch, sym): sym for sym in need}
    for f in as_completed(futures):
        sym, df = f.result()
        if df is not None:
            wh.store_daily_bars(df, if_exists="append")
            ok += 1
        else:
            fail += 1
logger.info(f"日线: {ok} ok, {fail} fail")

# 3. 覆盖率检查 — 数据不全不跑日报
logger.info("=== 覆盖率检查 ===")
COVERAGE_THRESHOLD = 0.95  # 至少 95% 活跃股有今日数据才跑日报
total_active = len(symbols)
covered = ok + (total_active - len(need))  # 已有 + 新增
coverage = covered / total_active if total_active > 0 else 0
logger.info(f"覆盖率: {covered}/{total_active} = {coverage:.1%} (阈值 {COVERAGE_THRESHOLD:.0%})")

if coverage < COVERAGE_THRESHOLD:
    logger.warning(f"覆盖率 {coverage:.1%} < {COVERAGE_THRESHOLD:.0%}，跳过日报生成")
else:
    logger.info("=== 日报 ===")
    from scripts.run_pipeline import run_pipeline
    success = run_pipeline(today)
    logger.info(f"日报: {'OK' if success else 'FAILED'}")
