"""数据转换模块 — 转换为 qlib 格式"""

from pathlib import Path

import pandas as pd

from config.settings import QLIB_DIR
from core.logger import get_logger

logger = get_logger("data.convert")

QLIB_COLUMNS = {
    "open": "$open",
    "close": "$close",
    "high": "$high",
    "low": "$low",
    "volume": "$volume",
    "amount": "$amount",
    "turnover": "$turnover",
    "vwap": "$vwap",
}


def to_qlib_format(df: pd.DataFrame) -> pd.DataFrame:
    """将标准 DataFrame 转为 qlib 格式列名（open → $open 等）"""
    mapping = QLIB_COLUMNS
    df = df.rename(columns={
        col: mapping[col] for col in df.columns if col in mapping
    })
    if "$factor" not in df.columns:
        df["$factor"] = 1.0
    return df


def build_qlib_calendar(start: str, end: str) -> list[str]:
    """生成交易日历"""
    dates = pd.date_range(start, end, freq="B")
    return [d.strftime("%Y-%m-%d") for d in dates]


def write_qlib_features(df: pd.DataFrame, freq: str = "day") -> Path:
    """将因子/特征数据写入 qlib 格式目录"""
    symbol = df["symbol"].iloc[0] if "symbol" in df.columns else "unknown"
    out = QLIB_DIR / "features" / symbol / freq
    out.mkdir(parents=True, exist_ok=True)
    for col in df.columns:
        if col.startswith("$") or col.startswith("factor"):
            s = df[["date", col]].dropna()
            s.to_parquet(out / f"{col}.parquet", index=False)
    logger.debug(f"Qlib特征写入: {symbol} → {out}")
    return out


def build_instruments(symbols: list[str]) -> dict:
    """生成 qlib instruments 配置"""
    instruments = {}
    for sym in symbols:
        market = "csi500"
        if sym.startswith("300"):
            market = "cyb"
        elif sym.startswith("688"):
            market = "kcb"
        elif sym.startswith("000"):
            market = "csi300"
        instruments[sym] = {"market": market}
    return instruments
