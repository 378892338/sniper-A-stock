"""本地数据仓库增量更新 — 多源自动降级，写入 SQLite。

数据源策略:
  - 个股日线 (OHLCV): Fetcher 降级链 (jqdata → akshare → eastmoney → sina → tushare → baostock)
  - 指数日线: AKShare
  - 申万行业指数: AKShare
  - 股票列表: AKShare
  - 交易日历: AKShare

下载策略:
  - Baostock 已弃用（服务端 IP 黑名单封禁，2026-06-23）
  - 使用 Fetcher 多源自动降级链替代
  - _probe_source_health 预探测，故障源提前标记，避免批量阶段浪费超时
  - 15 分钟总超时，防止管道永久阻塞
"""

import time
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
import akshare as ak

from data.local.warehouse import LocalDataWarehouse
from core.logger import get_logger

logger = get_logger("data.local.updater")

# 并发线程数
BAOSTOCK_MAX_WORKERS = 5
BAOSTOCK_BATCH_WRITE = 200

# 三大市场指数（与 backtest/data_loader.py 一致）
MARKET_INDEX_CODES: dict[str, str] = {
    "shanghai":  "sh000001",
    "shenzhen":  "sz399001",
    "chinext":   "sz399006",
    "csi300":    "sh000300",
}

# ETF 分类指数
ETF_INDEX_CODES: dict[str, str] = {
    "证券":     "sz399975", "银行":     "sz399986", "军工":     "sz399967",
    "新能源车": "sz399976", "消费":     "sh000932", "医药":     "sh000933",
    "酒":       "sz399997", "有色":     "sh000819", "煤炭":     "sz399998",
    "半导体":   "sz399678", "光伏":     "sz399395", "科技":     "sz399440",
    "汽车":     "sz399432",
}


# ── 股票列表 ──

def update_stock_list(warehouse: LocalDataWarehouse) -> None:
    """全量更新股票列表。优先 spot_em，失败则用 code_name。"""
    logger.info("更新股票列表...")
    df = _fetch_stock_list()
    warehouse.update_stock_list(df[["symbol", "name", "list_date", "status"]])
    warehouse.mark_updated("stock_list", len(df))
    logger.info(f"股票列表更新完成: {len(df)} 只")


def _fetch_stock_list() -> pd.DataFrame:
    """获取股票列表，带多源降级。保留上市日期（如有）。

    状态标记:
      - 'active': 正常交易股票
      - 'st': ST/*ST 股票
      - 'filtered': 北交所 (920xxx) 等不可获取的股票
    """
    try:
        raw = ak.stock_zh_a_spot_em()
        df = raw.rename(columns={"代码": "symbol", "名称": "name"})
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        df["status"] = "active"
        df.loc[df["name"].str.contains("ST", na=False), "status"] = "st"
        df.loc[df["symbol"].str.startswith("920"), "status"] = "filtered"
        df["list_date"] = (
            pd.to_datetime(raw["上市时间"], errors="coerce").dt.strftime("%Y-%m-%d")
            if "上市时间" in raw.columns else None
        )
        return df
    except Exception as e:
        logger.warning(f"spot_em 失败，降级到 code_name: {e}")

    raw = ak.stock_info_a_code_name()
    df = raw.rename(columns={"code": "symbol", "name": "name"})
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["status"] = "active"
    df.loc[df["name"].str.contains("ST", na=False), "status"] = "st"
    df.loc[df["symbol"].str.startswith("920"), "status"] = "filtered"
    df["list_date"] = None
    return df


# ── 交易日历 ──

def update_trade_calendar(warehouse: LocalDataWarehouse) -> None:
    """全量更新交易日历。"""
    logger.info("更新交易日历...")
    cal = ak.tool_trade_date_hist_sina()
    cal = cal.rename(columns={"trade_date": "date"})
    cal["date"] = pd.to_datetime(cal["date"]).dt.strftime("%Y-%m-%d")
    cal["is_trading"] = 1
    warehouse.update_trade_calendar(cal[["date", "is_trading"]])
    warehouse.mark_updated("trade_calendar", len(cal))
    logger.info(f"交易日历更新完成: {len(cal)} 条")


# ── 个股日线 ──

def _probe_source_health(symbol: str, start: str, end: str):
    """对 DATA_SOURCE_PREFERENCE 中每个源做一次健康探测。

    用一个代表性股票逐源尝试获取，失败源自动被 HealthTracker 标记不可用。
    必须在批量 fetch 之前执行——否则前几个股票每个故障源浪费 ~45s 超时+重试，
    批量 5000+ 只时累积数小时。

    Args:
        symbol: 探测用股票，默认 600436（片仔癀，全市场各源均有覆盖）
        start/end: 日期范围
    """
    from config.settings import DATA_SOURCE_PREFERENCE
    from shared.fetcher import Fetcher, SOURCE_ENDPOINTS
    from shared.retry import health_tracker
    import time as _time

    logger.info(f"  数据源健康探测 ({symbol})...")

    fetcher = Fetcher()
    for src_name in DATA_SOURCE_PREFERENCE:
        ep = SOURCE_ENDPOINTS.get(src_name)
        if ep and not health_tracker.is_available(ep):
            logger.info(f"    {src_name}: 已标记不可用，跳过")
            continue

        t0 = _time.time()
        try:
            df = fetcher.fetch_stock_daily(symbol, start, end, source_name=src_name)
            elapsed = _time.time() - t0
            if not df.empty:
                logger.info(f"    {src_name}: OK ({elapsed:.1f}s, {len(df)} 行)")
            else:
                logger.info(f"    {src_name}: 空数据 ({elapsed:.1f}s)")
        except Exception as e:
            elapsed = _time.time() - t0
            logger.warning(f"    {src_name}: 失败 ({elapsed:.1f}s) - {e}")

    # 汇总
    available = [
        n for n in DATA_SOURCE_PREFERENCE
        if (ep := SOURCE_ENDPOINTS.get(n)) and health_tracker.is_available(ep)
    ]
    unavailable = [n for n in DATA_SOURCE_PREFERENCE if n not in available]
    if available:
        logger.info(f"  可用: {available}")
    if unavailable:
        logger.info(f"  不可用(跳过): {unavailable}")
    if not available:
        logger.warning("  所有数据源均不可用！")


def _cross_validate_sample(symbols: list[str], start: str, end: str, warehouse, n: int = 50):
    """从已入库股票中随机抽样，用非主源做 close 价格交叉验证。

    设计约束：
      - 只抽样 n 只，不扫全量（控制耗时）
      - 校验源从 [sina, akshare] 随机选择（与主源 jqdata 不同栈）
      - 只比 close，不比 volume（不同源 volume 口径天然不同）
      - 不阻断管道，仅记录 WARNING

    Returns:
        dict: {corr, max_diff_pct, bad_samples: [...]}
    """
    import random as _random
    from data.normalizer import normalize
    from shared.retry import health_tracker

    # 选校验源：sina 或 akshare 随机，排除当前主源 jqdata
    candidates = ["sina", "akshare"]
    validate_src = _random.choice(candidates)

    sample = _random.sample(symbols, min(n, len(symbols)))
    n_sample = len(sample)
    logger.info(f"  交叉验证: {n_sample} 只, 校验源={validate_src}")

    from data.sources import get_source
    ds = get_source(validate_src)

    results = []
    for sym in sample:
        try:
            raw = ds.fetch_daily(sym, start, end)
            if raw.empty:
                results.append({"symbol": sym, "ok": False, "error": "空数据"})
                continue
            df = normalize(raw, sym, validate_src)
            if df.empty or "close" not in df.columns or len(df) < 3:
                results.append({"symbol": sym, "ok": False, "error": f"数据不足({len(df)}行)"})
                continue

            # 从仓库读主源数据做对比
            conn = warehouse._connect()
            try:
                import pandas as pd
                main = pd.read_sql(
                    "SELECT date, close FROM daily_bars WHERE symbol=? AND date>=? AND date<=?",
                    conn, params=(sym, start, end),
                )
            finally:
                conn.close()

            if main.empty:
                results.append({"symbol": sym, "ok": False, "error": "主源无数据"})
                continue

            main["date"] = pd.to_datetime(main["date"]).dt.strftime("%Y-%m-%d")
            df = df.reset_index() if isinstance(df.index, pd.DatetimeIndex) else df
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

            merged = main.merge(df[["date", "close"]], on="date", suffixes=("_main", "_val"))
            if len(merged) < 3:
                results.append({"symbol": sym, "ok": False, "error": f"仅有{len(merged)}个公共日期"})
                continue

            corr = merged["close_main"].corr(merged["close_val"])
            diff_pct = ((merged["close_val"] - merged["close_main"]).abs() / merged["close_main"]).max()
            results.append({
                "symbol": sym, "ok": corr > 0.99,
                "corr": round(corr, 4), "max_diff_pct": round(diff_pct * 100, 2),
                "common_days": len(merged),
            })
        except Exception as e:
            results.append({"symbol": sym, "ok": False, "error": str(e)[:80]})

    ok_count = sum(1 for r in results if r.get("ok"))
    bad = [r for r in results if not r.get("ok")]
    logger.info(
        f"  交叉验证完成: {ok_count}/{n_sample} 通过"
        + (f", {len(bad)} 异常" if bad else "")
    )
    if bad:
        for r in bad[:5]:
            logger.warning(f"    {r['symbol']}: {r}")


def _fetch_failed_with_fetcher(
    symbols: list[str], start: str, end: str, warehouse: LocalDataWarehouse,
    skip_validation: bool = False,
    stock_starts: dict[str, str] | None = None,
) -> tuple[int, dict[str, bool]]:
    """使用 Fetcher (DATA_SOURCE_PREFERENCE 动态降级) 批量获取个股日线。

    优化:
      1) 预探测 — 批量开始前 _probe_source_health
      2) 并发 — ThreadPoolExecutor
      3) 超时 — 15 分钟总时限
      4) 个股级起始日期 — stock_starts 参数支持增量补缺

    Args:
        stock_starts: {symbol: fetch_start} — 个股级起始日期，None 则全用全局 start

    Returns:
        (成功补拉的股票数, {symbol: success} 个股级结果字典)
    """
    from shared.fetcher import Fetcher, FetcherGuard
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time

    # 1) 健康预探测
    _probe_source_health("600436", start, end)

    # 2) 批量并发
    n_total = len(symbols)
    success = 0
    failed = 0
    deadline = _time.time() + 900
    n_workers = min(2, n_total) if n_total > 0 else 1
    _low_delay_guard = FetcherGuard(mean_delay=0.1, std_delay=0.05, burst_limit=50)

    logger.info(f"  批量下载: {n_total} 只, {n_workers} 线程 (超时 900s)")
    stop_event = threading.Event()
    per_stock_results: dict[str, bool] = {}
    _results_lock = threading.Lock()

    def _fetch_one(sym: str):
        if stop_event.is_set() or _time.time() > deadline:
            return None
        try:
            fetcher = Fetcher(guard=_low_delay_guard)
            # 个股级起始日期（增量模式）
            fstart = stock_starts.get(sym, start) if stock_starts else start
            df = fetcher.fetch_stock_daily(sym, fstart, end)
            if not df.empty:
                return df.assign(symbol=sym) if "symbol" not in df.columns else df
        except Exception as e:
            logger.debug(f"  {sym}: {e}")
        return None

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_fetch_one, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            stock_ok = False
            try:
                df = future.result()
                if df is not None:
                    warehouse.store_daily_bars(df, if_exists="append")
                    success += 1
                    stock_ok = True
                else:
                    failed += 1
            except Exception as e:
                logger.debug(f"  {sym} store: {e}")
                failed += 1
            with _results_lock:
                per_stock_results[sym] = stock_ok

            # 3) 超时守卫
            if _time.time() > deadline and not stop_event.is_set():
                stop_event.set()
                remaining = n_total - success - failed
                logger.warning(
                    f"  超时 (900s), 终止. ok={success} fail={failed} skip={remaining}"
                )

            # 4) 进度日志
            done = success + failed
            if done > 0 and done % 200 == 0:
                elapsed = _time.time() - (deadline - 900)
                logger.info(
                    f"  进度 {done}/{n_total} ({done/n_total*100:.0f}%) "
                    f"ok={success} fail={failed} {elapsed:.0f}s {done/elapsed:.1f}/s"
                )

    elapsed = min(900, _time.time() - (deadline - 900))
    logger.info(
        f"  完成: {success}/{n_total}, 失败 {failed}, "
        f"{elapsed:.0f}s, {n_total/max(elapsed,1):.1f}/s"
    )

    # 交叉验证：盘中模式跳过（留给 16:00 全量管道），全量模式做 50 只
    if not skip_validation:
        try:
            _cross_validate_sample(symbols, start, end, warehouse)
        except Exception as e:
            logger.warning(f"  交叉验证异常（不阻断）: {e}")

    return success, per_stock_results


def _next_trading_day(date_str: str) -> str:
    """返回 date_str 的下一个交易日。"""
    try:
        from data.local.warehouse import LocalDataWarehouse
        wh = LocalDataWarehouse()
        conn = wh._connect()
        try:
            cur = conn.execute(
                "SELECT date FROM trade_calendar WHERE date > ? AND is_trading = 1 ORDER BY date LIMIT 1",
                (date_str,)
            )
            row = cur.fetchone()
            return row[0] if row else date_str
        finally:
            conn.close()
    except Exception:
        return date_str


def _check_daily_bars_holes(wh, start: str, end: str) -> list[str]:
    """检测全局数据空洞——所有股票都缺失的日期。"""
    try:
        import pandas as _pd
        conn = wh._connect()
        try:
            df = _pd.read_sql(
                "SELECT date, COUNT(DISTINCT symbol) as cnt FROM daily_bars "
                "WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date",
                conn, params=(start, end),
            )
        finally:
            conn.close()
        wh2 = LocalDataWarehouse()
        conn2 = wh2._connect()
        try:
            cal = _pd.read_sql(
                "SELECT date FROM trade_calendar WHERE date BETWEEN ? AND ? AND is_trading = 1",
                conn2, params=(start, end),
            )
        finally:
            conn2.close()
        present = set(df["date"].tolist()) if not df.empty else set()
        trade_dates = sorted(cal["date"].tolist()) if not cal.empty else []
        missing = [d for d in trade_dates if d not in present]
        if missing:
            logger.warning(f"⚠ 全局数据空洞: {len(missing)} 天缺失: {missing[:5]}...")
        return missing
    except Exception as e:
        logger.debug(f"空洞检测异常（不阻断）: {e}")
        return []


def update_daily_bars_all(
    warehouse: LocalDataWarehouse,
    symbols: list[str] | None = None,
    start: str = "2000-01-01",
    end: str | None = None,
    batch_size: int = BAOSTOCK_BATCH_WRITE,
    max_workers: int = BAOSTOCK_MAX_WORKERS,
    delay: float = 0.0,
    skip_validation: bool = False,
    incremental: bool = True,
) -> None:
    """多线程并发更新个股日线（Fetcher 降级链，替代已弃用的 Baostock）。

    Fetcher 自动遍历 DATA_SOURCE_PREFERENCE:
      jqdata -> akshare -> eastmoney -> sina -> tushare -> baostock
    带 HealthTracker 健康追踪和 FetcherGuard 反爬保护。

    Args:
        warehouse: 数据仓库实例
        symbols: 指定个股列表 (None=全量 active)
        start: 起始日期 YYYY-MM-DD
        end:   结束日期 (默认今天)
        incremental: 增量模式（只补缺失部分，不全量重拉）
        skip_validation: 是否跳过交叉验证
        batch_size/max_workers/delay: 保留参数
    """
    if symbols is None:
        stock_df = warehouse.get_stock_list(status="active")
        symbols = stock_df["symbol"].tolist() if not stock_df.empty else []

    # 过滤 920xxx 北交所
    symbols = [s for s in symbols if not s.startswith("920")]

    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"更新个股日线 (Fetcher 降级链): {len(symbols)} 只, {start} ~ {end}")

    # 已有完整数据的跳过
    need_fetch: list[str] = []
    stock_starts: dict[str, str] = {}
    total_skipped = 0

    # 全局数据空洞检测（增量模式）
    holes = _check_daily_bars_holes(warehouse, start, end) if incremental else []
    hole_start = min(holes) if holes else None

    for sym in symbols:
        last = warehouse.get_last_date("daily_bars", "symbol", sym)
        if last and last >= end:
            total_skipped += 1
        else:
            need_fetch.append(sym)
            if incremental and last:
                # 增量模式：从上一个数据的下一个交易日开始
                stock_starts[sym] = _next_trading_day(last)
            elif incremental and hole_start:
                # 有全局空洞，从空洞开始补
                stock_starts[sym] = hole_start
            else:
                stock_starts[sym] = start

    if not need_fetch:
        logger.info(f"全部已是最新: {len(symbols)} 只跳过")
        warehouse.mark_updated("daily_bars", 0)
        return

    logger.info(f"需下载: {len(need_fetch)} 只 (增量模式)")

    # Fetcher 多线程并发下载（带个股级起始日期）
    _fetch_failed_with_fetcher(
        need_fetch, start, end, warehouse,
        skip_validation=skip_validation, stock_starts=stock_starts,
    )

    stats = warehouse.table_stats()
    actual_rows = stats.get("daily_bars", 0)
    logger.info(f"个股日线更新完成: 跳过 {total_skipped}")
    logger.info(f"仓库统计: daily_bars = {actual_rows} 行")
    warehouse.mark_updated("daily_bars", actual_rows)


# ── 指数日线 ──

def _fetch_index_daily_akshare(code: str, start: str, end: str) -> pd.DataFrame:
    """用 akshare 获取指数日线，按日期范围过滤。"""
    if code.startswith("sh") or code.startswith("sz") or code.startswith("bj"):
        symbol = code
    elif code.startswith("000"):
        symbol = f"sh{code}"
    else:
        symbol = f"sz{code}"

    df = ak.stock_zh_index_daily(symbol=symbol)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    mask = (df["date"] >= start) & (df["date"] <= end)
    return df[mask].reset_index(drop=True)


def update_market_indices(
    warehouse: LocalDataWarehouse,
    start: str = "2000-01-01",
    end: str | None = None,
    delay: float = 1.0,
) -> None:
    """增量更新市场指数与 ETF 分类指数。"""
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    all_indices = {**MARKET_INDEX_CODES, **ETF_INDEX_CODES}
    logger.info(f"更新指数数据: {len(all_indices)} 个, {start} ~ {end}")

    total = 0
    for name, code in all_indices.items():
        try:
            last = warehouse.get_last_date("index_daily", "name", name)
            fetch_start = last if last else start
            if last and last >= end:
                continue
            df = _fetch_index_daily_akshare(code, fetch_start, end)
            if df.empty:
                continue
            if last:
                df = df[df["date"] > last].copy()
            if df.empty:
                continue
            df["name"] = name
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")
            warehouse.store_index_daily(
                df[["name", "date", "open", "high", "low", "close", "volume"]],
                if_exists="append",
            )
            total += len(df)
            time.sleep(delay)
        except Exception as e:
            logger.warning(f"  {name}({code}) 获取失败: {e}")

    warehouse.mark_updated("index_daily", total)
    logger.info(f"指数更新完成: {total} 行")


# ── 申万行业指数 ──

def update_sw_indices(
    warehouse: LocalDataWarehouse,
    start: str = "2000-01-01",
    end: str | None = None,
    delay: float = 1.0,
) -> None:
    """增量更新申万一级行业指数。"""
    from data.industry import SW_INDEX_MAP

    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"更新申万行业指数: {len(SW_INDEX_MAP)} 个, {start} ~ {end}")

    list_df = pd.DataFrame([
        {"code": code, "name": name}
        for code, name in SW_INDEX_MAP.items()
    ])
    warehouse.update_sw_index_list(list_df)

    total = 0
    for code, name in SW_INDEX_MAP.items():
        try:
            last = warehouse.get_last_date("sw_index_daily", "code", code)
            fetch_start = last if last else start.replace("-", "")
            if last and last >= end:
                continue
            df = ak.index_hist_sw(
                symbol=code, period="day",
                start_date=fetch_start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
            if df is None or df.empty:
                continue
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            })
            df["date"] = pd.to_datetime(df["date"])
            df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
            if last:
                df = df[df["date"] > last].copy()
            if df.empty:
                continue
            df["code"] = code
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")
            warehouse.store_sw_index_daily(
                df[["code", "date", "open", "high", "low", "close", "volume"]],
                if_exists="append",
            )
            total += len(df)
            time.sleep(delay)
        except Exception as e:
            logger.debug(f"申万 {code}({name}) 获取失败: {e}")

    warehouse.mark_updated("sw_index_daily", total)
    logger.info(f"申万行业指数更新完成: {total} 行")


# ── 全量更新 ──

def update_all(
    warehouse: LocalDataWarehouse,
    start: str = "2000-01-01",
    end: str | None = None,
    include_daily_bars: bool = True,
    symbols: list[str] | None = None,
) -> None:
    """全量更新所有数据。"""
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"===== 本地数据仓库全量更新: {start} ~ {end} =====")

    update_stock_list(warehouse)
    update_trade_calendar(warehouse)
    update_market_indices(warehouse, start=start, end=end)
    update_sw_indices(warehouse, start=start, end=end)

    if include_daily_bars:
        update_daily_bars_all(warehouse, symbols=symbols, start=start, end=end)

    stats = warehouse.table_stats()
    logger.info("===== 更新完成 =====")
    for tbl, cnt in stats.items():
        logger.info(f"  {tbl}: {cnt} 行")


def main():
    """CLI 入口 - 通过 pipeline 一站式更新。"""
    import argparse

    parser = argparse.ArgumentParser(description="更新本地数据仓库")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--skip-daily", action="store_true",
                        help="跳过个股日线（节省时间）")
    parser.add_argument("--symbols", nargs="*", default=None,
                        help="指定个股代码列表")
    parser.add_argument("--calibrate", action="store_true",
                        help="更新后自动校准")
    args = parser.parse_args()

    from data.pipeline import download_and_update

    tables = ["stock_list", "trade_calendar", "index_daily", "sw_index_daily"]
    if not args.skip_daily:
        tables.append("daily_bars")

    warehouse = LocalDataWarehouse()
    download_and_update(
        warehouse,
        tables=tables,
        start=args.start,
        end=args.end,
        symbols=args.symbols,
        calibrate=args.calibrate,
    )


if __name__ == "__main__":
    main()
