"""数据获取模块 — 基于 akshare"""

import time
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

from config.settings import DATA_DIR, EXCLUDE_ST, EXCLUDE_NEW_IPO, IPO_DAYS_MIN
from core.logger import get_logger

logger = get_logger("data.fetch")


# ======== 股票池 ========

def fetch_stock_list() -> pd.DataFrame:
    """获取全部 A 股列表，过滤 ST 和上市不足 IPO_DAYS_MIN 天的新股"""
    df = ak.stock_zh_a_spot_em()
    df = df.rename(columns={
        "代码": "symbol",
        "名称": "name",
        "总市值": "total_mv",
        "流通市值": "float_mv",
    })
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)

    n_before = len(df)

    # ST 过滤
    if EXCLUDE_ST:
        df = df[~df["name"].str.contains("ST", na=False)]
        logger.info(f"ST过滤: {n_before - len(df)} 只被排除")

    # 新股过滤 (需上市日期字段)
    if EXCLUDE_NEW_IPO and "上市时间" in df.columns:
        n_before_ipo = len(df)
        df["ipo_date"] = pd.to_datetime(df["上市时间"], errors="coerce")
        min_date = pd.Timestamp.now() - pd.Timedelta(days=IPO_DAYS_MIN)
        df = df[df["ipo_date"] <= min_date]
        logger.info(f"新股过滤: {n_before_ipo - len(df)} 只被排除 (上市<{IPO_DAYS_MIN}天)")
    elif EXCLUDE_NEW_IPO:
        logger.warning("未找到上市时间字段，跳过新股过滤")

    logger.info(f"股票池最终: {len(df)} 只 (共排除 {n_before - len(df)} 只)")
    return df[["symbol", "name", "total_mv", "float_mv"]]


# ======== 日线行情 ========

def fetch_daily(symbol: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
    """获取单只股票日线数据（东方财富源）"""
    try:
        df = ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust=adjust,
        )
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "换手率": "turnover",
        })
        df["date"] = pd.to_datetime(df["date"])
        df["symbol"] = symbol
        return df.set_index("date")
    except Exception as e:
        logger.debug(f"fetch_daily({symbol}) 失败: {e}")
        return pd.DataFrame()


def fetch_daily_tx(symbol: str, start: str, end: str) -> pd.DataFrame:
    """获取单只股票日线（优先腾讯源，volume 缺失时退化为 EM 源）"""
    code = f"sz{symbol}" if symbol.startswith(("0", "3")) else f"sh{symbol}"
    try:
        df = ak.stock_zh_a_hist_tx(
            symbol=code,
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
        )
        df = df.rename(columns={
            "date": "date", "open": "open", "close": "close",
            "high": "high", "low": "low",
        })
        if "volume" not in df.columns:
            df["volume"] = np.nan
        else:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df["symbol"] = symbol
        # TX 端点 volume 全 NaN(含空字符串被 to_numeric 转为 NaN) → 降级到 EM 端点
        if df["volume"].isna().all():
            logger.debug(f"fetch_daily_tx({symbol}) volume 缺失，降级到 EM 源")
            em = fetch_daily(symbol, start, end)
            if not em.empty and "volume" in em.columns and em["volume"].notna().any():
                common_dates = df.index.intersection(em.index)
                df.loc[common_dates, "volume"] = em.loc[common_dates, "volume"]
        return df.set_index("date")
    except Exception as e:
        logger.debug(f"fetch_daily_tx({symbol}) 失败: {e}")
        return pd.DataFrame()


# ======== 批量获取 ========

def fetch_all_daily(
    symbols: list[str],
    start: str,
    end: str,
    sleep: float = 0.3,
) -> dict[str, pd.DataFrame]:
    """批量获取日线，返回 {symbol: DataFrame}"""
    result = {}
    failed = 0
    for i, sym in enumerate(symbols):
        df = fetch_daily_tx(sym, start, end)
        if not df.empty:
            result[sym] = df
        else:
            failed += 1
        if (i + 1) % 50 == 0:
            logger.info(f"已获取 {i+1}/{len(symbols)} (成功 {len(result)}, 失败 {failed})")
        if sleep > 0:
            time.sleep(sleep)
    logger.info(f"批量获取完成: {len(result)} 成功, {failed} 失败")
    return result


def save_raw(df: pd.DataFrame, name: str) -> Path:
    """保存原始数据到 parquet"""
    path = DATA_DIR / f"{name}.parquet"
    df.to_parquet(path)
    return path


def load_raw(name: str) -> pd.DataFrame:
    """加载原始数据"""
    return pd.read_parquet(DATA_DIR / f"{name}.parquet")
