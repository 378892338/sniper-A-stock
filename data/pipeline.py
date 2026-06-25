"""统一数据管道 — 下载 → 校准 → 写入 → 校验 → 导出回测缓存。

所有数据从 SQLite 仓库读写，确保口径一致：
  - download_and_update: 全量/增量下载多数据源 → 自动去重 → 写入仓库
  - export_backtest_snapshot: 仓库 → parquet 回测缓存（带快照时间戳元数据）
  - sync_stock_to_cache: 单次回测前快速同步（增量导出）
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd

from data.local.warehouse import LocalDataWarehouse
from data.local.schema import DB_FILE
from core.logger import get_logger

logger = get_logger("data.pipeline")

TableName = Literal["stock_list", "daily_bars", "index_daily",
                     "sw_index_daily", "trade_calendar"]


# ═══════════════════════════════════════════
# 下载与更新
# ═══════════════════════════════════════════


def download_and_update(
    warehouse: LocalDataWarehouse,
    tables: list[TableName] | None = None,
    start: str = "2000-01-01",
    end: str | None = None,
    symbols: list[str] | None = None,
    calibrate: bool = True,
    batch_size: int = 50,
    delay: float = 0.2,
) -> dict[str, int]:
    """统一数据下载入口 — 下载 → 校准 → 写入仓库。

    Args:
        warehouse: 数据仓库实例
        tables: 要更新的表 (默认全量)
        start/end: 日期范围
        symbols: 指定个股列表 (None=所有 active)
        calibrate: 下载后是否自动校验
        batch_size/delay: 个股下载参数

    Returns: {table_name: 写入行数}
    """
    from data.local.updater import (
        update_stock_list,
        update_trade_calendar,
        update_market_indices,
        update_sw_indices,
        update_daily_bars_all,
    )

    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    if tables is None:
        tables = ["stock_list", "trade_calendar", "index_daily",
                   "sw_index_daily", "daily_bars"]

    logger.info(f"===== 数据管道更新开始: {tables} ({start} ~ {end}) =====")
    results: dict[str, int] = {}

    # 启动时自动检查 schema 对齐（因子需求 vs 数据库列）
    from data.local.schema import ensure_schema
    schema_report = ensure_schema(warehouse)
    if schema_report.get("missing"):
        logger.info(f"schema 自动扩展完成: {schema_report['missing']}")
    elif schema_report.get("added"):
        logger.info(f"schema 自动扩展完成: {schema_report['added']}")

    if "stock_list" in tables:
        n_before = len(warehouse.get_stock_list(status=None))
        update_stock_list(warehouse)
        n_after = len(warehouse.get_stock_list(status=None))
        results["stock_list"] = n_after - n_before
        logger.info(f"  股票列表: {n_before} → {n_after}")

    if "trade_calendar" in tables:
        update_trade_calendar(warehouse)
        results["trade_calendar"] = 0

    if "index_daily" in tables:
        update_market_indices(warehouse, start=start, end=end)
        results["index_daily"] = 0  # 行数在函数内已统计

    if "sw_index_daily" in tables:
        update_sw_indices(warehouse, start=start, end=end)
        results["sw_index_daily"] = 0

    if "daily_bars" in tables:
        update_daily_bars_all(
            warehouse,
            symbols=symbols,
            start=start,
            end=end,
            batch_size=batch_size,
            delay=delay,
        )
        results["daily_bars"] = 0

    # 下载后校准
    if calibrate:
        _calibrate_after_update(warehouse, tables)

    logger.info("===== 数据管道更新完成 =====")
    return results


def _calibrate_after_update(
    warehouse: LocalDataWarehouse,
    tables: list[TableName],
) -> None:
    """更新后自动执行轻量级校验。"""
    from data.quality import (
        check_data_freshness,
        validate_warehouse,
    )

    print("\n--- 更新后数据校验 ---")

    # 新鲜度检查
    for tbl in tables:
        r = check_data_freshness(warehouse, tbl)
        if not r.passed:
            logger.warning(f"新鲜度检查未通过: {r.target} — {r.checks[0].detail}")

    if "daily_bars" in tables:
        bad_pool = _quick_volume_check(warehouse)
        if bad_pool:
            logger.warning(f"volume=0 异常个股: {len(bad_pool)} 只 (前5: {bad_pool[:5]})")

    print("--- 校验完成 ---\n")


def _quick_volume_check(
    warehouse: LocalDataWarehouse,
    max_samples: int = 100,
    threshold: float = 0.10,
) -> list[str]:
    """快速扫描 volume=0 占比超阈值的个股。"""
    stock_df = warehouse.get_stock_list(status="active")
    symbols = stock_df["symbol"].tolist()[:max_samples]
    bad = []
    for sym in symbols:
        df = warehouse.get_daily_bars(sym, start="2025-01-01")
        if df is None or df.empty:
            continue
        n = len(df)
        zero = int((df["volume"] == 0).sum())
        if n > 0 and zero / n > threshold:
            bad.append(sym)
    return bad


# ═══════════════════════════════════════════
# 导出回测缓存
# ═══════════════════════════════════════════


def export_backtest_snapshot(
    warehouse: LocalDataWarehouse,
    cache_dir: str | Path,
    symbols: list[str] | None = None,
    start: str = "2000-01-01",
    end: str | None = None,
) -> Path:
    """将 SQLite 仓库数据导出为 parquet 回测缓存。

    产生的 parquet 文件与 ``DataStore.from_parquet_cache()`` 兼容：
      - market_daily_{name}.parquet  — 市场指数日线
      - etf_daily_{name}.parquet     — ETF 分类指数日线
      - stock_{symbol}_daily.parquet — 个股日线
      - sw_index_daily.parquet       — 申万行业指数日线（全量）
      - snapshot.json                — 快照元数据

    Returns: cache_dir Path
    """
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    from backtest.data_loader import MARKET_INDEX_CODES, ETF_INDEX_CODES

    n_stocks = 0

    required_ohlc = ["open", "high", "low", "close", "volume"]

    # 市场指数
    for name, code in MARKET_INDEX_CODES.items():
        df = warehouse.get_index_daily(name, start=start, end=end)
        if df is not None and not df.empty:
            for col in required_ohlc:
                if col not in df.columns:
                    logger.warning(f"指数 {name}: 缺少列 '{col}', 用 0 填充")
                    df[col] = float("nan")
            df.to_parquet(cache_dir / f"market_daily_{name}.parquet")
            n_stocks += 1

    # ETF 指数
    for name in ETF_INDEX_CODES:
        df = warehouse.get_index_daily(name, start=start, end=end)
        if df is not None and not df.empty:
            for col in required_ohlc:
                if col not in df.columns:
                    logger.warning(f"ETF {name}: 缺少列 '{col}', 用 0 填充")
                    df[col] = float("nan")
            df.to_parquet(cache_dir / f"etf_daily_{name}.parquet")
            n_stocks += 1

    # 个股日线
    if symbols is None:
        stock_df = warehouse.get_stock_list(status="active")
        symbols = stock_df["symbol"].tolist() if not stock_df.empty else []

    n_exported = 0
    for sym in symbols:
        df = warehouse.get_daily_bars(sym, start=start, end=end)
        if df is None or df.empty:
            continue
        if len(df) < 200:  # 过滤数据过短的（刚上市/退市）
            continue
        # 补齐列
        stock_cols = ["open", "high", "low", "close", "volume", "amount"]
        for col in stock_cols:
            if col not in df.columns:
                logger.warning(f"个股 {sym}: 缺少列 '{col}', 用 0 填充")
                df[col] = 0.0
        df["symbol"] = sym
        df.to_parquet(cache_dir / f"stock_{sym}_daily.parquet")
        n_exported += 1

    # 申万行业指数（全量，用于回测 fetch_sw_sector_indices）
    sw_list = warehouse.get_sw_index_list()
    sw_rows = []
    for _, row in sw_list.iterrows():
        code = row["code"]
        df = warehouse.get_sw_index_daily(code, start=start, end=end)
        if df is not None and not df.empty:
            df = df.copy()
            df["code"] = code
            df["name"] = row["name"]
            sw_rows.append(df.reset_index())
    if sw_rows:
        pd.concat(sw_rows, ignore_index=True).to_parquet(
            cache_dir / "sw_index_daily.parquet")

    # 导出 consolidated daily_bars.parquet（全量个股日线，按 date 分区）
    # 用于 DataStore.from_consolidated_cache() 快速加载
    if symbols:
        all_bars_list = []
        for sym in symbols:
            df = warehouse.get_daily_bars(sym, start=start, end=end)
            if df is not None and not df.empty and len(df) >= 200:
                df = df.reset_index()
                df["symbol"] = sym
                all_bars_list.append(df)
        if all_bars_list:
            all_bars = pd.concat(all_bars_list, ignore_index=True)
            all_bars.to_parquet(cache_dir / "daily_bars.parquet")
            logger.info(f"consolidated daily_bars: {len(all_bars)} 行, {all_bars['symbol'].nunique()} 只")

    # 快照元数据
    snapshot_meta = {
        "exported_at": datetime.now().isoformat(),
        "start": start,
        "end": end,
        "n_stocks": n_exported,
        "n_indices": n_stocks,
        "source_db": str(warehouse.db_path),
    }
    with open(cache_dir / "snapshot.json", "w") as f:
        json.dump(snapshot_meta, f, indent=2)

    logger.info(f"导出回测快照: {n_exported} 个股, {n_stocks} 指数 → {cache_dir}")
    return cache_dir


def sync_stock_to_cache(
    warehouse: LocalDataWarehouse,
    cache_dir: str | Path,
    symbols: list[str] | None = None,
    start: str = "2000-01-01",
    end: str | None = None,
    force_reload: bool = False,
) -> int:
    """增量同步个股数据到回测缓存。

    只更新已过期或缺失的 parquet 文件。用于回测前快速同步。

    Returns: 同步的个股数
    """
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    cache_dir = Path(cache_dir)

    if symbols is None:
        stock_df = warehouse.get_stock_list(status="active")
        symbols = stock_df["symbol"].tolist() if not stock_df.empty else []

    n_synced = 0
    for sym in symbols:
        parquet_path = cache_dir / f"stock_{sym}_daily.parquet"

        # 检查是否需要更新
        if not force_reload and parquet_path.exists():
            try:
                existing = pd.read_parquet(parquet_path)
                last_existing = existing.index.max() if isinstance(existing.index, pd.DatetimeIndex) else pd.to_datetime(existing["date"]).max()
                if last_existing >= end:
                    continue  # 已是最新
            except Exception:
                pass  # 文件损坏，重新导出

        df = warehouse.get_daily_bars(sym, start=start, end=end)
        if df is None or df.empty:
            continue
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col not in df.columns:
                df[col] = 0.0
        df["symbol"] = sym
        df.to_parquet(parquet_path)
        n_synced += 1

    if n_synced > 0:
        logger.info(f"增量同步: {n_synced} 个股")
    return n_synced


# ═══════════════════════════════════════════
# 一站式回测准备
# ═══════════════════════════════════════════


def prepare_backtest(
    cache_dir: str | Path,
    symbols: list[str] | None = None,
    start: str = "2019-01-01",
    end: str | None = None,
    max_age_days: int = 3,
    update_first: bool = True,
    calibrate: bool = True,
) -> Path:
    """回测前一站式准备: (可选更新) → 校验 → 导出缓存。

    这是回测入口的标准调用点。

    Args:
        cache_dir: 回测缓存目录
        symbols: 个股列表 (None=全部 active)
        start/end: 日期范围
        max_age_days: 数据新鲜度阈值
        update_first: 是否先检查并更新过期数据
        calibrate: 是否自动校验

    Returns: cache_dir Path
    """
    cache_dir = Path(cache_dir)
    warehouse = LocalDataWarehouse()

    if update_first:
        needs_update = any(
            warehouse.needs_update(tbl, max_age_days)
            for tbl in ["daily_bars", "index_daily", "sw_index_daily"]
        )
        if needs_update:
            logger.info("数据过期，启动自动更新...")
            download_and_update(
                warehouse,
                start="2000-01-01",
                end=end,
                calibrate=calibrate,
            )

    if calibrate:
        from data.quality import validate_warehouse
        reports = validate_warehouse(
            warehouse, symbols=symbols, start=start, end=end)
        n_fail = sum(1 for r in reports if not r.passed)
        if n_fail > 0:
            logger.warning(f"数据校验: {n_fail}/{len(reports)} 项未通过")

    # 导出缓存
    export_backtest_snapshot(
        warehouse, cache_dir, symbols=symbols, start=start, end=end)

    return cache_dir
