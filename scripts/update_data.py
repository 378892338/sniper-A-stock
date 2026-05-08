"""增量数据更新 — 每日收市后运行，自动补足缺失数据"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import akshare as ak

from backtest.data_loader import (
    MARKET_INDEX_CODES, ETF_INDEX_CODES,
    fetch_index_daily,
)
from data.market_broad import resample_to_weekly, resample_to_monthly
from backtest.runner import precompute_all_stocks
from core.logger import get_logger

logger = get_logger("scripts.update_data")

CACHE_DIR = Path("data/raw/_cache/backtest")
LAST_RUN_FILE = Path("output/.last_data_update")
STOCK_LIST_CACHE = CACHE_DIR / "stock_list.parquet"

# 交易日历缓存，避免重复请求
_TRADE_CAL = None


def _get_trade_cal() -> set:
    """获取A股交易日历"""
    global _TRADE_CAL
    if _TRADE_CAL is not None:
        return _TRADE_CAL
    try:
        cal = ak.tool_trade_date_hist_sina()
        # 取 2019 到现在的交易日
        cal = cal[cal["trade_date"] >= "2019-01-01"]
        _TRADE_CAL = set(cal["trade_date"].astype(str))
    except Exception:
        logger.warning("获取交易日历失败，用周一到周五近似")
        _TRADE_CAL = None
    return _TRADE_CAL


def is_trading_day(dt: datetime = None) -> bool:
    """判断是否交易日"""
    if dt is None:
        dt = datetime.now()
    ds = dt.strftime("%Y-%m-%d")
    cal = _get_trade_cal()
    if cal is None:
        return dt.weekday() < 5
    return ds in cal


def get_last_update() -> str:
    """读取最后成功更新时间"""
    if LAST_RUN_FILE.exists():
        return LAST_RUN_FILE.read_text(encoding="utf-8").strip()
    return "2019-01-01"


def write_last_update(ds: str):
    """写入最后更新时间"""
    LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_FILE.write_text(ds, encoding="utf-8")


def update_index_data(last_date: str, today: str):
    """增量更新市场指数 + ETF 分类指数"""
    logger.info("--- 更新指数数据 ---")

    # 市场指数
    for name, code in MARKET_INDEX_CODES.items():
        path = CACHE_DIR / f"market_daily_{name}.parquet"
        if path.exists():
            existing = pd.read_parquet(path)
            existing_last = str(existing.index[-1])[:10]
            if existing_last >= today:
                logger.info(f"  {name}: 已最新 ({existing_last})")
                continue
            start = existing_last
        else:
            start = last_date

        time.sleep(1)
        df = fetch_index_daily(code, start, today)
        if df.empty:
            continue

        if path.exists():
            df = pd.concat([existing, df]).drop_duplicates().sort_index()
        df.to_parquet(path)
        logger.info(f"  {name}: {len(df)} 条")

        # 重算周/月线
        weekly = resample_to_weekly(df)
        monthly = resample_to_monthly(df)
        weekly.to_parquet(CACHE_DIR / f"market_weekly_{name}.parquet")
        monthly.to_parquet(CACHE_DIR / f"market_monthly_{name}.parquet")

    # benchmark
    bm_path = CACHE_DIR / "benchmark_csi300.parquet"
    mkt_path = CACHE_DIR / "market_daily_csi300.parquet"
    if mkt_path.exists():
        df = pd.read_parquet(mkt_path)
        df["close"].to_frame("close").to_parquet(bm_path)

    # ETF
    for name, code in ETF_INDEX_CODES.items():
        d_path = CACHE_DIR / f"etf_daily_{name}.parquet"
        if d_path.exists():
            existing = pd.read_parquet(d_path)
            existing_last = str(existing.index[-1])[:10]
            if existing_last >= today:
                continue
            start = existing_last
        else:
            start = last_date

        time.sleep(1)
        df = fetch_index_daily(code, start, today)
        if df.empty:
            continue

        if d_path.exists():
            df = pd.concat([existing, df]).drop_duplicates().sort_index()
        df.to_parquet(d_path)

        weekly = resample_to_weekly(df)
        weekly.to_parquet(CACHE_DIR / f"etf_weekly_{name}.parquet")

    logger.info("指数更新完成")


def _update_one_stock(sym: str, last_date: str, today: str) -> tuple[str, bool]:
    """更新一只股票的日线数据"""
    path = CACHE_DIR / f"stock_{sym}_daily.parquet"
    try:
        if path.exists():
            existing = pd.read_parquet(path)
            existing_last = str(existing.index[-1])[:10]
            if existing_last >= today:
                return sym, True  # 已最新
            start = existing_last
        else:
            return sym, False  # 不再新增股票（只更新已有的）

        time.sleep(0.15)
        df = ak.stock_zh_a_hist_tx(sym, start=start, end=today)
        if df is None or df.empty:
            return sym, True

        # 统一格式
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "振幅": "amplitude",
            "涨跌幅": "pct_change", "涨跌额": "change",
            "换手率": "turnover",
        })
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df = df[["open", "high", "low", "close", "volume"]].dropna()

        if path.exists():
            df = pd.concat([existing, df]).drop_duplicates().sort_index()
        # 保留 symbol 列
        df["symbol"] = sym
        df.to_parquet(path)
        return sym, True
    except Exception as e:
        logger.warning(f"  {sym} 更新失败: {e}")
        return sym, False


def update_stock_data(last_date: str, today: str):
    """增量更新所有已有缓存的个股"""
    logger.info("--- 更新个股数据 ---")

    stock_files = sorted(CACHE_DIR.glob("stock_*_daily.parquet"))
    symbols = []
    for f in stock_files:
        sym = f.stem.replace("stock_", "").replace("_daily", "")
        symbols.append(sym)

    logger.info(f"待更新: {len(symbols)} 只股票")

    updated = 0
    failed = 0
    batch_size = 50

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_update_one_stock, sym, last_date, today): sym for sym in batch}
            for f in as_completed(futures):
                sym, ok = f.result()
                if ok:
                    updated += 1
                else:
                    failed += 1

        if (i + batch_size) % 200 == 0 or i + batch_size >= len(symbols):
            logger.info(f"  进度: {min(i+batch_size, len(symbols))}/{len(symbols)} ({updated} ok, {failed} fail)")

    logger.info(f"个股更新完成: {updated} OK, {failed} FAIL")


def update_l3_scores(today: str):
    """增量更新 L3 评分（只算新月份）"""
    logger.info("--- 更新 L3 评分 ---")
    l3_path = CACHE_DIR / "l3_scores_all.parquet"

    if l3_path.exists():
        l3 = pd.read_parquet(l3_path)
        existing_months = set(l3["month"].unique())
    else:
        l3 = pd.DataFrame()
        existing_months = set()

    # 需要计算的月份 = 从已有数据到今天的月份
    stock_files = sorted(CACHE_DIR.glob("stock_*_daily.parquet"))
    stock_data: dict[str, pd.DataFrame] = {}
    for f in stock_files:
        try:
            df = pd.read_parquet(f)
            if len(df) >= 200:
                sym = df["symbol"].iloc[0] if "symbol" in df.columns else f.stem.replace("stock_", "").replace("_daily", "")
                stock_data[sym] = df
        except Exception:
            continue

    # 看所有股票的最新月份
    new_months = set()
    for sym, df in stock_data.items():
        last_m = str(df.index[-1])[:7]
        if last_m not in existing_months:
            new_months.add(last_m)

    if not new_months:
        logger.info("无新月份需计算")
        return

    logger.info(f"需计算月份: {sorted(new_months)}")
    new_rows = []
    for sym, df in stock_data.items():
        last_m = str(df.index[-1])[:7]
        if last_m in new_months:
            from backtest.runner import _precompute_stock_monthly
            rows = _precompute_stock_monthly(sym, df)
            new_rows.extend(rows)
            time.sleep(0.001)

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if not l3.empty:
            l3 = pd.concat([l3, new_df]).drop_duplicates(
                subset=["symbol", "month"]
            ).sort_values(["symbol", "month"])
        else:
            l3 = new_df
        l3.to_parquet(l3_path, index=False)
        logger.info(f"L3 更新完成: {len(l3)} 条记录 (+{len(new_rows)} 新)")


def main():
    logger.info("=== 增量数据更新 ===")

    today = datetime.now().strftime("%Y-%m-%d")
    last_date = get_last_update()

    if last_date >= today:
        logger.info(f"数据已最新 (last={last_date}, today={today})")
        # 但还是检查一下有没有交易日被遗漏
        cal = _get_trade_cal()
        if cal is None:
            logger.info("跳过检查（无交易日历）")
            return
        trade_dates = sorted(d for d in cal if last_date <= d <= today)
        if len(trade_dates) <= 1:
            logger.info("无新交易日数据")
            return

    logger.info(f"更新区间: {last_date} -> {today}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    update_index_data(last_date, today)
    update_stock_data(last_date, today)
    update_l3_scores(today)

    write_last_update(today)
    logger.info(f"=== 更新完成 (last={today}) ===")


if __name__ == "__main__":
    main()
