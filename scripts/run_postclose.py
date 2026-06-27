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
from core.logger import get_logger

logger = get_logger("scripts.postclose")
today = datetime.now().strftime("%Y-%m-%d")

wh = LocalDataWarehouse()

# 交易日守卫
if not wh.is_trading_day(today):
    logger.info(f"非交易日 {today}，跳过")
    raise SystemExit(0)

# 1. 指数更新
logger.info("=== 指数 ===")
update_market_indices(wh, start=today, end=today)
update_sw_indices(wh, start=today, end=today)

# 2. 数据源探针（仅用于信息输出）
logger.info("=== 数据源探针 ===")
from data.local.updater import _probe_source_health
_probe_source_health("600436", today, today)

logger.info("=== 日线补更 ===")
stock_df = wh.get_stock_list(status="active")
symbols = [s for s in stock_df["symbol"].tolist() if not s.startswith("920")]
need = [s for s in symbols if not wh.get_last_date("daily_bars", "symbol", s) or wh.get_last_date("daily_bars", "symbol", s) < today]
logger.info(f"需补 {len(need)} 只, workers=5")

ok = fail = 0

def _fetch(sym):
    """直连优先 → 失败随时切 Fetcher 降级链"""
    # 直连 10jqka（同花顺，速度快）
    try:
        from data.sources.hexin import HexinDataSource
        ds = HexinDataSource()
        df = ds.fetch_daily(sym, today, today)
        if df is not None and not df.empty:
            if "symbol" not in df.columns:
                df = df.assign(symbol=sym)
            return sym, df
    except Exception:
        pass
    # 直连均失败 → Fetcher 降级链
    try:
        from shared.fetcher import Fetcher, FetcherGuard
        f = Fetcher(guard=FetcherGuard(mean_delay=0.05, std_delay=0.02, burst_limit=100))
        df = f.fetch_stock_daily(sym, today, today)
        if df is not None and not df.empty:
            if "symbol" not in df.columns:
                df = df.assign(symbol=sym)
            return sym, df
    except Exception:
        pass
    return sym, None

with ThreadPoolExecutor(max_workers=5) as pool:
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
