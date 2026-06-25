"""数据统一规格层 — 因子驱动格式，股票代码为锚

核心原则:
  因子需要什么 → Normalizer 就必须输出什么 → 数据库就必须存什么。

Normalizer 不做数据源的 ABC 或路由（那由 data/sources/__init__.py 处理），
只做格式转换：将任意数据源的原始 DataFrame 转为因子需要的统一格式。
"""

import pandas as pd
import numpy as np

from core.logger import get_logger

logger = get_logger("data.normalizer")

# ── 列名映射（源列名 → 标准列名）──
COLUMN_MAP: dict[str, dict[str, str]] = {
    "eastmoney": {
        "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume", "成交额": "amount",
        "换手率": "turnover",
    },
    "sina": {
        "date": "date", "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume", "amount": "amount",
    },
    "akshare": {
        "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume", "成交额": "amount",
        "换手率": "turnover", "振幅": "amplitude", "涨跌幅": "pct_chg",
    },
    "baostock": {
        "date": "date", "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume", "amount": "amount",
        "turnover": "turnover", "pctChg": "pct_chg",
    },
    "tushare": {
        "trade_date": "date", "open": "open", "high": "high", "low": "low",
        "close": "close", "vol": "volume", "amount": "amount",
    },
    "jqdata": {
        "date": "date", "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume", "money": "amount",
    },
    "mootdx": {
        "date": "date", "open": "open", "high": "high", "low": "low",
        "close": "close", "vol": "volume", "amount": "amount",
        "code": "symbol",
    },
    "akshare_daily": {
        "date": "date", "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume", "amount": "amount",
        "turnover": "turnover",
    },
}

# ── 体积/金额换算 ──
VOLUME_SCALE = {
    "eastmoney": 1,       # 东方财富已为股数
    "sina": 1,
    "akshare": 100,       # 手 → 股
    "baostock": 1,
    "tushare": 1,
    "jqdata": 1,           # 聚宽已为股数
    "mootdx": 1,           # 通达信已为股数
    "akshare_daily": 1,    # 新浪已为股数
}
AMOUNT_SCALE = {
    "eastmoney": 1,       # 元
    "sina": 1,
    "akshare": 1,
    "baostock": 1 / 1000, # 千元 → 元
    "tushare": 1 / 1000,  # 千元 → 元
    "jqdata": 1,           # 聚宽已为元
    "mootdx": 1,
    "akshare_daily": 1,    # 新浪已为元
}


def normalize(raw_df: pd.DataFrame, symbol: str, source: str) -> pd.DataFrame:
    """将任意数据源的原始 DataFrame 转为统一格式。

    处理:
      1. 列名映射（源列名 → 标准列名）
      2. 单位换算（手→股，千元→元）
      3. 缺失字段填充 NaN（不是 0）
      4. 确保所有 P0 字段存在
      5. 极值裁剪
      6. 日期排序

    Args:
        raw_df: 数据源原始 DataFrame
        symbol: 股票代码（用于日志/校验）
        source: 数据源名称（用于列映射和单位换算）

    Returns:
        统一格式的 DataFrame，包含标准列名
    """
    if raw_df.empty:
        return pd.DataFrame()

    df = raw_df.copy()

    # 0. 如果 date 在 index 中 → reset_index 拿回列
    if "date" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index()

    # 1. 列名映射
    col_map = COLUMN_MAP.get(source, {})
    # 反向映射：找标准列在源中的对应列名
    reverse_map = _build_reverse_map(df.columns, col_map)
    df = df.rename(columns=reverse_map)

    # 2. 只保留已知的标准列
    known_cols = ["date", "open", "high", "low", "close", "volume", "amount", "turnover"]
    keep = [c for c in known_cols if c in df.columns]
    df = df[keep].copy()

    # 3. volume 单位换算
    vol_scale = VOLUME_SCALE.get(source, 1)
    if "volume" in df.columns and vol_scale != 1:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * vol_scale

    # 4. amount 单位换算
    amt_scale = AMOUNT_SCALE.get(source, 1)
    if "amount" in df.columns and amt_scale != 1:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce") * amt_scale

    # 5. 数值列统一转为 float
    numeric_cols = ["open", "high", "low", "close", "volume", "amount", "turnover"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 6. 确保 P0 字段存在（缺失填 NaN）
    from sniper.config import FACTOR_REQUIRED_FIELDS
    for col in FACTOR_REQUIRED_FIELDS:
        if col not in df.columns:
            df[col] = float("nan")

    # 7. 极值裁剪（避免脏数据）
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            # 非正数 → NaN
            df.loc[df[col] <= 0, col] = float("nan")
            # > 10000 元 → NaN（A股不可能）
            df.loc[df[col] > 10000, col] = float("nan")

    for col in ["volume", "amount"]:
        if col in df.columns:
            df.loc[df[col] < 0, col] = 0.0

    # 8. OHLC 一致性兜底
    if all(c in df.columns for c in ["high", "low", "open", "close"]):
        df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
        df["low"] = df[["open", "high", "low", "close"]].min(axis=1)

    # 9. 日期处理
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        # 过滤无效日期
        df = df.dropna(subset=["date"])
        df = df.sort_values("date").reset_index(drop=True)

    return df


def _build_reverse_map(source_cols, col_map):
    """构建从源列名 → 标准列名的映射（只取源中存在的列）。"""
    reverse = {}
    for src_col, std_col in col_map.items():
        if src_col in source_cols:
            reverse[src_col] = std_col
    return reverse


def get_required_schema() -> list[str]:
    """返回当前因子层需要的字段列表。"""
    try:
        from sniper.config import FACTOR_REQUIRED_FIELDS
        return FACTOR_REQUIRED_FIELDS.copy()
    except ImportError:
        return ["open", "high", "low", "close", "volume", "amount"]


def get_desired_schema() -> list[str]:
    """返回当前因子层期望的字段列表（有最好，没有也可以）。"""
    try:
        from sniper.config import FACTOR_DESIRED_FIELDS
        return FACTOR_DESIRED_FIELDS.copy()
    except ImportError:
        return ["turnover"]
