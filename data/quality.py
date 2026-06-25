"""数据质量校准与验证 — 口径一致、时间一致、质量一致。

提供三种校验层级：
  1. 单表校验 (validate_daily_bars / validate_index_daily)
  2. 跨源比对 (compare_volume_sources)
  3. 仓库完整性检查 (check_data_freshness / inspect_volume_zeros)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from core.logger import get_logger

logger = get_logger("data.quality")

# 质量分阈值
CLOSE_NAN_MAX = 0.0       # close 不允许 NaN
VOLUME_ZERO_MAX = 0.05     # volume=0 天数占比上限
OHLC_INVALID_MAX = 0.01    # high<max(open,close) 天数占比上限
DATE_GAP_MAX_DAYS = 5      # 最大允许数据间隔（自然日）


@dataclass
class QualityCheck:
    """一条质量检查结果。"""
    name: str
    passed: bool
    detail: str = ""
    severity: str = "INFO"  # INFO / WARN / ERROR


@dataclass
class QualityReport:
    """一组质量检查的汇总报告。"""
    target: str = ""           # 检查对象：symbol / index name
    checks: list[QualityCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.severity == "ERROR")

    @property
    def has_warning(self) -> bool:
        return any(not c.passed for c in self.checks if c.severity == "WARN")

    def add(self, name: str, passed: bool, detail: str = "",
            severity: str = "ERROR") -> None:
        self.checks.append(QualityCheck(name, passed, detail, severity))

    def print(self) -> None:
        icon = "✓" if self.passed else ("!" if self.has_warning else "✗")
        print(f"  [{icon}] {self.target}")
        for c in self.checks:
            status = "✓" if c.passed else "✗"
            print(f"    {status} [{c.severity}] {c.name}: {c.detail}")


# ═══════════════════════════════════════════
# 单表校验
# ═══════════════════════════════════════════


def validate_daily_bars(df: pd.DataFrame, symbol: str | None = None) -> QualityReport:
    """校验个股日线完整性。

    检查项:
      - close 无 NaN
      - volume 零值占比不超阈值
      - OHLC 关系合理: high >= max(open,close), low <= min(open,close)
      - 日期连续无大间隔
    """
    report = QualityReport(target=symbol or "daily_bars")

    if df is None or df.empty:
        report.add("empty", False, "数据为空")
        return report

    close = df["close"]
    n = len(close)

    # close NaN
    nan_mask = close.isna()
    n_nan = int(nan_mask.sum())
    report.add("close_nan", n_nan <= CLOSE_NAN_MAX * n,
               f"close NaN: {n_nan}/{n} 行",
               "ERROR" if n_nan > 0 else "INFO")

    if n_nan == n:
        return report

    # volume 零值
    if "volume" in df.columns:
        vol = df["volume"]
        zero_vol = (vol == 0) | vol.isna()
        n_zero = int(zero_vol.sum())
        report.add("volume_zero", n_zero / n <= VOLUME_ZERO_MAX,
                   f"volume=0: {n_zero}/{n} ({n_zero/n*100:.1f}%)",
                   "ERROR" if n_zero / n > VOLUME_ZERO_MAX else "WARN" if n_zero > 0 else "INFO")

    # OHLC 合理性
    if all(c in df.columns for c in ["open", "high", "low"]):
        invalid_high = (df["high"] < df[["open", "close"]].max(axis=1)).sum()
        invalid_low = (df["low"] > df[["open", "close"]].min(axis=1)).sum()
        total_invalid = invalid_high + invalid_low
        report.add("ohlc_valid", total_invalid / n <= OHLC_INVALID_MAX,
                   f"high<max(oc) {invalid_high} 天, low>min(oc) {invalid_low} 天",
                   "WARN" if total_invalid > 0 else "INFO")

    # 日期间隔（取日期的天数差，非自然日连续）
    if isinstance(df.index, pd.DatetimeIndex) and len(df.index) >= 2:
        gaps = df.index.to_series().diff().dt.days.iloc[1:]
        max_gap = int(gaps.max()) if not gaps.empty else 0
        report.add("date_gap", max_gap <= DATE_GAP_MAX_DAYS,
                   f"最大日期间隔: {max_gap} 天 (含周末)",
                   "WARN" if max_gap > DATE_GAP_MAX_DAYS else "INFO")

    return report


def validate_index_daily(df: pd.DataFrame, name: str | None = None) -> QualityReport:
    """校验指数日线完整性。逻辑同 validate_daily_bars，但要宽松一些（指数可能缺 volume）。"""
    report = QualityReport(target=name or "index_daily")

    if df is None or df.empty:
        report.add("empty", False, "数据为空")
        return report

    close = df["close"]
    n = len(close)

    nan_mask = close.isna()
    n_nan = int(nan_mask.sum())
    report.add("close_nan", n_nan == 0, f"close NaN: {n_nan}/{n} 行",
               "ERROR" if n_nan > 0 else "INFO")

    if n_nan == n:
        return report

    if "volume" in df.columns:
        vol = df["volume"]
        zero_vol = (vol == 0) | vol.isna()
        n_zero = int(zero_vol.sum())
        # 指数 volume = 0 很常见，只 WARN 不 ERROR
        report.add("volume_zero", n_zero / n <= 0.5,
                   f"volume=0: {n_zero}/{n} ({n_zero/n*100:.1f}%)",
                   "WARN" if n_zero / n > 0.3 else "INFO")

    if isinstance(df.index, pd.DatetimeIndex) and len(df.index) >= 2:
        gaps = df.index.to_series().diff().dt.days.iloc[1:]
        max_gap = int(gaps.max()) if not gaps.empty else 0
        report.add("date_gap", max_gap <= DATE_GAP_MAX_DAYS,
                   f"最大日期间隔: {max_gap} 天",
                   "WARN" if max_gap > DATE_GAP_MAX_DAYS else "INFO")

    return report


# ═══════════════════════════════════════════
# 跨源比对
# ═══════════════════════════════════════════


def compare_volume_sources(
    symbol: str,
    start: str = "2025-01-01",
    end: str | None = None,
) -> dict:
    """对比 EM 端点和 TX 端点的 volume 数据。

    返回包含两源数据行数、相关系数、差异天数的字典。
    """
    import akshare as ak

    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    code = f"sh{symbol}" if symbol.startswith(("6", "9")) else f"sz{symbol}"
    sf = start.replace("-", "")
    ef = end.replace("-", "")

    result: dict = {"symbol": symbol, "em_days": 0, "tx_days": 0,
                    "volume_corr": None, "volume_diff_days": 0, "max_diff_pct": None}

    em_vol = None
    tx_vol = None

    # EM 端点
    try:
        em = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=sf, end_date=ef)
        if not em.empty:
            em = em.rename(columns={"日期": "date", "成交量": "volume"})
            em["date"] = pd.to_datetime(em["date"])
            em = em.set_index("date")
            result["em_days"] = len(em)
            em_vol = em["volume"].replace(0, np.nan)
    except Exception as e:
        logger.debug(f"EM volume 对比失败 {symbol}: {e}")

    # TX 端点
    try:
        tx = ak.stock_zh_a_hist_tx(symbol=code, start_date=sf, end_date=ef)
        if not tx.empty:
            date_col = next((c for c in ["date", "日期"] if c in tx.columns), None)
            if date_col is None:
                return result
            tx = tx.rename(columns={date_col: "date"})
            tx["date"] = pd.to_datetime(tx["date"])
            tx = tx.set_index("date")
            result["tx_days"] = len(tx)
            tx_vol = tx["volume"].replace(0, np.nan)
    except Exception as e:
        logger.debug(f"TX volume 对比失败 {symbol}: {e}")

    # 任一源为空则无法对比
    if em_vol is None or tx_vol is None:
        return result

    # 对齐
    common = em_vol.index.intersection(tx_vol.index)
    if len(common) < 5:
        return result

    e_v = em_vol.loc[common]
    t_v = tx_vol.loc[common]
    corr = e_v.corr(t_v)
    result["volume_corr"] = round(float(corr), 4) if not pd.isna(corr) else None

    diff = (e_v - t_v).abs()
    pct = diff / e_v.replace(0, np.nan)
    result["volume_diff_days"] = int((diff > 0).sum())
    result["max_diff_pct"] = round(float(pct.max()), 4) if not pct.dropna().empty else None

    return result


# ═══════════════════════════════════════════
# 仓库完整性
# ═══════════════════════════════════════════


def check_data_freshness(
    warehouse,
    table: str,
    max_age_days: int = 5,
) -> QualityReport:
    """检查数据新鲜度。"""
    report = QualityReport(target=f"freshness:{table}")

    last = warehouse.get_last_update(table) if hasattr(warehouse, "get_last_update") else None
    if last is None:
        report.add("never_updated", False, f"{table} 从未更新", "ERROR")
        return report

    last_dt = datetime.fromisoformat(last)
    age = (datetime.now() - last_dt).days
    report.add("stale", age <= max_age_days,
               f"上次更新: {last} ({age} 天前, 阈值 {max_age_days} 天)",
               "ERROR" if age > max_age_days else "INFO")
    return report


def inspect_volume_zeros(
    warehouse,
    symbols: list[str] | None = None,
    start: str = "2019-01-01",
    end: str | None = None,
    threshold: float = 0.05,
) -> pd.DataFrame:
    """扫描 stocks，找出 volume=0 占比超阈值的个股。

    返回 DataFrame: symbol, total_days, zero_days, zero_ratio
    """
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    if symbols is None:
        stock_df = warehouse.get_stock_list(status="active")
        symbols = stock_df["symbol"].tolist() if not stock_df.empty else []

    rows = []
    for sym in symbols:
        df = warehouse.get_daily_bars(sym, start=start, end=end)
        if df is None or df.empty:
            continue
        n = len(df)
        zero = int((df["volume"] == 0).sum())
        ratio = zero / n
        if ratio > threshold:
            rows.append({"symbol": sym, "total_days": n,
                         "zero_days": zero, "zero_ratio": round(ratio, 4)})

    return pd.DataFrame(rows).sort_values("zero_ratio", ascending=False)


def repair_volume_from_em(warehouse, symbol: str) -> int:
    """对某只股票，用 EM 端点补回 TX 端缺失的 volume。

    Returns: 修复的行数
    """
    import akshare as ak

    df = warehouse.get_daily_bars(symbol)
    if df is None or df.empty:
        return 0

    # 找出 volume=0 的日期
    zero_mask = df["volume"] == 0
    if not zero_mask.any():
        return 0

    start = df.index[0].strftime("%Y%m%d")
    end = df.index[-1].strftime("%Y%m%d")
    code = f"sh{symbol}" if symbol.startswith(("6", "9")) else f"sz{symbol}"

    try:
        em = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=start, end_date=end)
        if em.empty:
            return 0
        em = em.rename(columns={"日期": "date", "成交量": "volume"})
        em["date"] = pd.to_datetime(em["date"])
        em_vol = em.set_index("date")["volume"].replace(0, np.nan)
    except Exception as e:
        logger.warning(f"EM repair 失败 {symbol}: {e}")
        return 0

    # 对齐补回
    fix_dates = df.index[zero_mask].intersection(em_vol.index)
    if fix_dates.empty:
        return 0

    fixes = []
    for d in fix_dates:
        v = em_vol.loc[d]
        if pd.notna(v) and v > 0:
            fixes.append({"symbol": symbol, "date": d.strftime("%Y-%m-%d"),
                          "volume": float(v)})

    if not fixes:
        return 0

    fix_df = pd.DataFrame(fixes)
    conn = warehouse._connect()
    try:
        for _, row in fix_df.iterrows():
            conn.execute(
                "UPDATE daily_bars SET volume = ? WHERE symbol = ? AND date = ?",
                (row["volume"], row["symbol"], row["date"]),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"volume 修复 {symbol}: {len(fixes)} 行")
    return len(fixes)


# ═══════════════════════════════════════════
# 批量校验入口
# ═══════════════════════════════════════════


def validate_warehouse(
    warehouse,
    symbols: list[str] | None = None,
    start: str = "2019-01-01",
    end: str | None = None,
    tables: list[str] | None = None,
) -> list[QualityReport]:
    """对仓库指定表执行批量校验。

    Args:
        warehouse: LocalDataWarehouse 实例
        symbols: 个股列表 (None=全部 active)
        start/end: 日期范围
        tables: 要检查的表 (默认 [daily_bars, index_daily, sw_index_daily])

    Returns: QualityReport 列表
    """
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    if tables is None:
        tables = ["daily_bars", "index_daily", "sw_index_daily"]

    reports = []

    # 新鲜度检查
    for tbl in tables:
        r = check_data_freshness(warehouse, tbl)
        reports.append(r)
        r.print()

    # 个股日线校验 (抽样)
    if "daily_bars" in tables:
        if symbols is None:
            stock_df = warehouse.get_stock_list(status="active")
            symbols = stock_df["symbol"].tolist() if not stock_df.empty else []
            symbols = symbols[:200]  # 自动生成列表抽样，避免扫描全部
        bad_count = 0
        for sym in symbols:
            df = warehouse.get_daily_bars(sym, start=start, end=end)
            r = validate_daily_bars(df, sym)
            if not r.passed or r.has_warning:
                r.print()
                bad_count += 1
            reports.append(r)
        logger.info(f"个股日线校验: {len(symbols)} 只, {bad_count} 只有问题")

    # 指数日线校验
    if "index_daily" in tables:
        names = warehouse.get_all_index_names()
        for name in names:
            df = warehouse.get_index_daily(name, start=start, end=end)
            r = validate_index_daily(df, name)
            if not r.passed or r.has_warning:
                r.print()
            reports.append(r)

    return reports


# ═══════════════════════════════════════════
# Per-Symbol 校验（2026-06-05 新增）
# ═══════════════════════════════════════════


def validate_download(df: pd.DataFrame, symbol: str) -> list[str]:
    """对单只股票的下载结果做前置校验。

    入参:
      df: Normalizer 输出后的统一格式 DataFrame
      symbol: 股票代码

    返回:
      问题列表，空列表 = 通过
        如: ["close 有 3 天 NaN", "volume 全部为 0", "OHLC 关系异常 5 天"]

    结果决定:
      问题列表为空 → 入库
      有问题 → 丢弃该源数据，降级到下一源重试
    """
    problems: list[str] = []

    if df is None or df.empty:
        return ["数据为空"]

    if "close" not in df.columns:
        return ["缺少 close 列"]

    n = len(df)

    # 数据量 >= 5
    if n < 5:
        problems.append(f"数据不足: {n} 行")

    # close 无 NaN
    nan_close = int(df["close"].isna().sum())
    if nan_close > 0:
        problems.append(f"close NaN: {nan_close} 天")

    # OHLC 关系合理
    if all(c in df.columns for c in ["high", "low", "open", "close"]):
        invalid_high = int((df["high"] < df[["open", "close"]].max(axis=1)).sum())
        invalid_low = int((df["low"] > df[["open", "close"]].min(axis=1)).sum())
        if invalid_high > 0:
            problems.append(f"high<max(oc): {invalid_high} 天")
        if invalid_low > 0:
            problems.append(f"low>min(oc): {invalid_low} 天")

    # volume 零值检测
    if "volume" in df.columns:
        vol = df["volume"]
        zero_vol = int((vol == 0).sum())
        if zero_vol == n:
            problems.append("volume 全部为 0")
        elif zero_vol / n > 0.5:
            problems.append(f"volume=0 占比 {zero_vol/n*100:.0f}%")

    # open/close 非零
    if "open" in df.columns and (df["open"] == 0).any():
        problems.append("open 含零值")
    if "close" in df.columns and (df["close"] == 0).any():
        problems.append("close 含零值")

    return problems


def validate_symbols(
    warehouse,
    symbols: list[str],
    start: str = "2019-01-01",
    end: str | None = None,
) -> dict[str, dict]:
    """对指定的股票列表逐只校验，返回每只的结果字典。

    每只股票的检查项:
      - 日期覆盖完整性: 是否有数据
      - volume 零值占比
      - close 极值检测
      - OHLC 合理性
      - 数据新鲜度

    Args:
        warehouse: LocalDataWarehouse 实例
        symbols: 要检查的股票列表
        start/end: 日期范围

    Returns:
        {symbol: {"ok": bool, "problems": [str], "rows": int, "last_date": str}}
    """
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    results: dict[str, dict] = {}
    for sym in symbols:
        df = warehouse.get_daily_bars(sym, start=start, end=end)
        if df is None or df.empty:
            results[sym] = {"ok": False, "problems": ["无数据"],
                            "rows": 0, "last_date": None}
            continue

        problems = validate_download(df, sym)
        last_date = str(df.index[-1])[:10] if isinstance(df.index, pd.DatetimeIndex) else ""

        results[sym] = {
            "ok": len(problems) == 0,
            "problems": problems,
            "rows": len(df),
            "last_date": last_date,
        }

    return results


def cross_validate_symbol(
    fetcher,
    symbol: str,
    sources: list[str] | None = None,
    start: str = "2025-01-01",
    end: str | None = None,
) -> dict:
    """对同一只股票，从多个源分别获取，比对 close 一致性。

    Args:
        fetcher: Fetcher 实例
        symbol: 股票代码
        sources: 要比对的数据源列表（None=常用源）
        start/end: 日期范围

    Returns:
        {"symbol": str, "sources": {}, "close_corr": float, "conclusion": str}
    """
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")
    if sources is None:
        sources = ["eastmoney", "sina", "akshare"]

    result: dict = {
        "symbol": symbol,
        "sources": {},
        "close_corr": None,
        "conclusion": "未知",
    }

    dfs: dict[str, pd.DataFrame] = {}
    for src in sources:
        try:
            df = fetcher.fetch_stock_daily(symbol, start, end, source_name=src)
            if df is not None and not df.empty and "close" in df.columns:
                dfs[src] = df
                result["sources"][src] = {
                    "rows": len(df),
                    "last_close": float(df["close"].iloc[-1]) if not df.empty else None,
                }
            else:
                result["sources"][src] = {"rows": 0, "last_close": None}
        except Exception as e:
            result["sources"][src] = {"rows": 0, "last_close": None, "error": str(e)}

    # 比对 close 相关系数
    close_dfs = []
    for src, df in dfs.items():
        if "close" in df.columns and "date" in df.columns:
            d = df[["date", "close"]].copy()
            d = d.rename(columns={"close": f"close_{src}"})
            d["date"] = pd.to_datetime(d["date"])
            close_dfs.append(d)

    if len(close_dfs) >= 2:
        merged = close_dfs[0]
        for d in close_dfs[1:]:
            merged = merged.merge(d, on="date", how="outer")
        merged = merged.dropna()
        if len(merged) >= 5:
            cols = [c for c in merged.columns if c.startswith("close_")]
            corr = merged[cols].corr()
            if len(cols) == 2:
                result["close_corr"] = round(float(corr.iloc[0, 1]), 4)

    if result["close_corr"] and result["close_corr"] > 0.99:
        result["conclusion"] = "一致"
    elif result["close_corr"] and result["close_corr"] > 0.95:
        result["conclusion"] = "基本一致"
    elif result["close_corr"]:
        result["conclusion"] = "不一致"

    return result


def detect_price_anomalies(
    df: pd.DataFrame,
    window: int = 5,
    threshold: float = 0.30,
) -> list[str]:
    """检测 close 价格异常：距 window 日均线超过 ±threshold。

    Args:
        df: DataFrame 需包含 close 列
        window: 移动平均窗口
        threshold: 偏离阈值（默认 30%）

    Returns:
        异常日期列表 ["YYYY-MM-DD", ...]
    """
    if df is None or df.empty or "close" not in df.columns:
        return []

    close = df["close"]
    ma = close.rolling(window).mean()
    deviation = (close - ma).abs() / ma.replace(0, float("nan"))
    anomaly_mask = deviation > threshold

    if isinstance(df.index, pd.DatetimeIndex):
        return [str(d)[:10] for d in df.index[anomaly_mask]]
    return []


def auto_repair(
    warehouse,
    fetcher,
    symbols: list[str],
    source_priority: list[str] | None = None,
) -> dict:
    """对指定股票列表，校验后发现问题自动修复。

    每只股票的修复策略:
      - 完全缺失 → 从优先级最高的源获取 → 规格化 → 入库
      - volume=0 → 从有 volume 的源只补 volume 列
      - 数据不新鲜 → 增量更新该股票

    Args:
        warehouse: LocalDataWarehouse 实例
        fetcher: Fetcher 实例
        symbols: 要修复的股票列表
        source_priority: 源优先级（None=默认）

    Returns:
        {"repaired": int, "failed": int, "skipped": int, "details": [str]}
    """
    if source_priority is None:
        source_priority = ["eastmoney", "sina", "akshare"]

    repaired = 0
    failed = 0
    skipped = 0
    details: list[str] = []

    for sym in symbols:
        # 先检查现有数据
        df = warehouse.get_daily_bars(sym)
        if df is not None and not df.empty:
            # volume=0 修复
            zero_vol = (df["volume"] == 0).sum()
            total = len(df)
            if zero_vol > 0 and zero_vol / total > 0.5:
                logger.info(f"volume 修复: {sym} (zero={zero_vol}/{total})")
                n = repair_volume_from_em(warehouse, sym)
                if n > 0:
                    repaired += 1
                    details.append(f"{sym}: volume 修复 {n} 行")
                else:
                    failed += 1
                    details.append(f"{sym}: volume 修复失败")
            else:
                skipped += 1
        else:
            # 完全缺失，用 Fetcher 获取
            logger.info(f"缺失数据: {sym}，尝试 Fetcher 获取")
            ok = False
            for src in source_priority:
                try:
                    new_df = fetcher.fetch_stock_daily(sym, source_name=src)
                    if new_df is not None and not new_df.empty:
                        new_df["symbol"] = sym
                        warehouse.store_daily_bars(new_df, if_exists="append")
                        ok = True
                        repaired += 1
                        details.append(f"{sym}: 从 {src} 补全 {len(new_df)} 行")
                        break
                except Exception as e:
                    logger.debug(f"{sym}: {src} 失败: {e}")
            if not ok:
                failed += 1
                details.append(f"{sym}: 所有源均失败")

    return {"repaired": repaired, "failed": failed, "skipped": skipped, "details": details}


def generate_quality_section(
    warehouse,
    fetcher,
    symbols: list[str],
    start: str = "2000-01-01",
    end: str | None = None,
) -> str:
    """对指定股票列表生成日报第 8 节「数据质量」。

    如果全部正常，简化为一行。
    如果有问题，逐只列出。

    Args:
        warehouse: LocalDataWarehouse 实例
        fetcher: Fetcher 实例
        symbols: 策略用到的股票列表
        start/end: 日期范围

    Returns:
        Markdown 格式的质量报告
    """
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d")

    results = validate_symbols(warehouse, symbols, start=start, end=end)
    ok_count = sum(1 for v in results.values() if v["ok"])
    total = len(results)

    problems = {sym: v for sym, v in results.items() if not v["ok"]}

    # 健康状态
    health = fetcher.health_status() if hasattr(fetcher, "health_status") else []
    health_str = ""
    if health:
        health_lines = [f"    {h['endpoint']}: {'❌ 不可用' if not h['available'] else '✅ 正常'}"
                       for h in health]
        health_str = "\n" + "\n".join(health_lines)

    if not problems:
        return (
            f"## 8. 数据质量  ✅ 全部正常"
            f"（{total} 只，数据至 {end}）{health_str}"
        )

    lines = [f"## 8. 数据质量\n"]
    lines.append(f"策略依赖: {total} 只（{ok_count} 正常, {len(problems)} 异常）\n")

    for sym, info in problems.items():
        prob_str = ", ".join(info["problems"])
        lines.append(f"  - {sym}: ❌ {prob_str}")
        if info["last_date"]:
            lines[-1] += f" (最新: {info['last_date']})"

    lines.append("")
    lines.append(f"今日数据: {'⚠ 待补' if problems else '✅ 已更新至 ' + end}")

    if health:
        lines.append("\n数据源健康:")
        for h in health:
            lines.append(f"  {h['endpoint']}: {'❌ 不可用' if not h['available'] else '✅ 正常'}")

    return "\n".join(lines)
