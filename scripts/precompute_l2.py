"""L2 因子预计算 — 向量化批量计算所有股票所有交易日的 9 个技术因子

2026-06-29: 新增 bottom_fractal / volume_reversal / low_volatility，删除 turnover_score。

用法:
  python scripts/precompute_l2.py

输出:
  outputs/precomputed/l2_factors/{date}.parquet
  — 每交易日一个文件，含当日全市场股票的因子值

性能: 2557 只 × 7 年 ≈ 2 分钟（向量化）
回测加速: 读取 parquet → <10ms/天，总提速 >50x
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import numpy as np
from datetime import datetime

from sniper.config import STOCK as CFG
from data.local.warehouse import LocalDataWarehouse
from core.logger import get_logger

logger = get_logger("scripts.precompute_l2")

OUTPUT_DIR = Path("outputs/precomputed/l2_factors")


def compute_factors_for_stock(sym: str, df: pd.DataFrame) -> pd.DataFrame:
    """计算单只股票全历史的技术因子（向量化）。

    2026-06-29: 新增 bottom_fractal / volume_reversal / low_volatility，删除 turnover_score。
    """
    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    n = len(df)
    dates = df.index

    # 1. 趋势因子: MA(N) 偏离
    trend = pd.Series(close, index=dates).rolling(CFG.momentum_window).mean()
    trend = ((close / trend) - 1) * 200 + 50
    trend = trend.clip(0, 100)

    # 2. 量能因子: volume / MA5_vol × 50
    if "volume" in df.columns:
        vol_ma5 = df["volume"].rolling(5).mean()
        volume = (df["volume"] / vol_ma5.replace(0, np.nan)) * 50
        volume = volume.clip(0, 100)
    else:
        volume = pd.Series(50.0, index=dates)

    # 3. MACD 因子
    ema12 = pd.Series(close, index=dates).ewm(span=12, adjust=False).mean()
    ema26 = pd.Series(close, index=dates).ewm(span=26, adjust=False).mean()
    macd = ((ema12 - ema26) / ema26.replace(0, np.nan)) * 500 + 50
    macd = macd.clip(0, 100)

    # 4. RSI 因子
    delta = pd.Series(np.diff(close, prepend=close[0]), index=dates)
    gain = delta.clip(lower=0).rolling(CFG.rsi_window).mean()
    loss = (-delta.clip(upper=0)).rolling(CFG.rsi_window).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.clip(0, 100)

    # 5. 市值因子
    if "amount" in df.columns:
        log_amt = np.log(df["amount"].clip(lower=1))
        mcap = ((25 - log_amt) / 10 * 100).clip(0, 100)
    else:
        mcap = pd.Series(50.0, index=dates)

    # 6. 底分型因子（2026-06-29 新增）: low[i-1] > low[i] < low[i+1] + 有实体
    low_s = pd.Series(low, index=dates)
    high_s = pd.Series(high, index=dates)
    close_s = pd.Series(close, index=dates)
    bottom_fractal = pd.Series(50.0, index=dates)
    condition = (
        (low_s.shift(1) > low_s)                            # low[i-1] > low[i] (左侧更高)
        & (low_s < low_s.shift(-1))                          # low[i] < low[i+1] (右侧更高)
        & (close_s > low_s + (high_s - low_s) * 0.3)         # 中间K线有实体
    )
    bottom_fractal = bottom_fractal.where(~condition, 100.0)

    # 7. 量价反转因子（2026-06-29 新增）: 5 日量价背离
    volume_reversal = pd.Series(50.0, index=dates)
    if "volume" in df.columns:
        vol_s = df["volume"]
        ret_5 = close_s / close_s.shift(5) - 1
        vol_ma5 = vol_s.rolling(5).mean()
        vol_ratio = vol_s / vol_ma5.replace(0, np.nan)
        price_dir = np.sign(ret_5)
        vol_dir = np.sign(vol_ratio - 1)
        divergence = price_dir * vol_dir
        volume_reversal = (50 - divergence * 30).clip(0, 100)

    # 8. 低波动率因子（2026-06-29 新增）: 20 日波动率取反
    daily_ret = close_s.pct_change()
    vol_20 = daily_ret.rolling(20).std()
    low_volatility = (50 - vol_20 * 500).clip(0, 100)

    result = pd.DataFrame({
        "symbol": sym,
        "date": dates,
        "trend": trend,
        "volume": volume,
        "macd": macd,
        "rsi": rsi,
        "market_cap": mcap,
        "bottom_fractal": bottom_fractal,
        "volume_reversal": volume_reversal,
        "low_volatility": low_volatility,
    }, index=dates)

    return result.reset_index(drop=True)


def precompute_all(start: str = "2019-01-01", end: str | None = None) -> int:
    """全量预计算 L2 因子。"""
    end = end or datetime.now().strftime("%Y-%m-%d")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    wh = LocalDataWarehouse()
    conn = wh._connect()

    logger.info("加载 daily_bars...")
    try:
        bars = pd.read_sql(
            "SELECT symbol, date, close, high, low, volume, amount "
            "FROM daily_bars WHERE date >= ? AND date <= ? ORDER BY symbol, date",
            conn, params=(start, end),
        )
    finally:
        conn.close()

    logger.info(f"  {len(bars)} 行, {bars['symbol'].nunique()} 只")

    bars["date"] = pd.to_datetime(bars["date"], format="mixed")

    all_parts = []
    total = bars["symbol"].nunique()

    for i, (sym, grp) in enumerate(bars.groupby("symbol")):
        grp = grp.sort_values("date")
        grp = grp.set_index("date")

        if len(grp) == 0:
            continue

        try:
            fdf = compute_factors_for_stock(sym, grp)
            all_parts.append(fdf)
        except Exception as e:
            logger.debug(f"  {sym} 计算失败: {e}")

        if (i + 1) % 500 == 0:
            logger.info(f"  进度: {i+1}/{total}")

    if not all_parts:
        logger.warning("无因子数据")
        return 0

    full = pd.concat(all_parts, ignore_index=True)
    logger.info(f"因子表: {len(full)} 行")

    # 按日期分片写入
    full["date_str"] = full["date"].dt.strftime("%Y-%m-%d")
    n_files = 0
    for date_str, grp in full.groupby("date_str"):
        grp = grp.drop(columns=["date", "date_str"])
        grp = grp.set_index("symbol")
        grp.to_parquet(OUTPUT_DIR / f"{date_str}.parquet")
        n_files += 1

    trade_dates = sorted(full["date_str"].unique())
    pd.DataFrame({"date": trade_dates}).to_parquet(OUTPUT_DIR / "_trade_dates.parquet")

    logger.info(f"完成: {n_files} 个文件, {len(trade_dates)} 天")
    return n_files


if __name__ == "__main__":
    precompute_all()
