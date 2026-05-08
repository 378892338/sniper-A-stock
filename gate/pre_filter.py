"""前置过滤器 — ST/退市/流动性/次新股排除"""

import pandas as pd

from core.logger import get_logger

logger = get_logger("gate.pre_filter")


def filter_st_stocks(df: pd.DataFrame) -> pd.DataFrame:
    """过滤 ST / *ST 股票"""
    before = len(df)
    name_col = None
    for col in ["name", "名称", "股票名称"]:
        if col in df.columns:
            name_col = col
            break
    if name_col is None:
        logger.warning("未找到股票名称列，跳过ST过滤")
        return df
    result = df[~df[name_col].str.contains("ST", na=False)]
    logger.info(f"ST过滤: {before - len(result)} 只排除, 剩余 {len(result)}")
    return result


def filter_delisting(df: pd.DataFrame, delist_list: list[str] | None = None) -> pd.DataFrame:
    """过滤退市整理期股票"""
    if delist_list is None:
        return df
    before = len(df)
    sym_col = None
    for col in ["symbol", "代码"]:
        if col in df.columns:
            sym_col = col
            break
    if sym_col is None:
        return df
    result = df[~df[sym_col].isin(delist_list)]
    if before != len(result):
        logger.info(f"退市过滤: {before - len(result)} 只排除")
    return result


def filter_new_stocks(df: pd.DataFrame, min_trading_days: int = 60) -> pd.DataFrame:
    """过滤上市不满 min_trading_days 个交易日的次新股"""
    before = len(df)
    ipo_col = None
    for col in ["ipo_date", "上市时间", "上市日期"]:
        if col in df.columns:
            ipo_col = col
            break
    if ipo_col is None:
        logger.warning("未找到上市日期列，跳过次新股过滤")
        return df
    cutoff = pd.Timestamp.now() - pd.offsets.BDay(min_trading_days)
    df[ipo_col] = pd.to_datetime(df[ipo_col], errors="coerce")
    result = df[df[ipo_col] <= cutoff]
    logger.info(f"次新股过滤: {before - len(result)} 只排除 (上市<{min_trading_days}天)")
    return result


def filter_low_liquidity(df: pd.DataFrame, min_daily_amount: float = 20_000_000) -> pd.DataFrame:
    """过滤日均成交额 < min_daily_amount 的股票（单位：元）"""
    before = len(df)
    amount_col = None
    for col in ["amount", "amount_mean_20", "成交额", "日均成交额"]:
        if col in df.columns:
            amount_col = col
            break
    if amount_col is None:
        logger.warning("未找到成交额列，跳过流动性过滤")
        return df
    result = df[df[amount_col] >= min_daily_amount]
    logger.info(f"流动性过滤: {before - len(result)} 只排除 (日均成交额<{min_daily_amount/1e4:.0f}万)")
    return result


def check_consecutive_limit_down(hist_df: pd.DataFrame, lookback: int = 30) -> bool:
    """检查近 lookback 交易日是否有连续跌停（一字板）"""
    if hist_df.empty or "pct_chg" not in hist_df.columns:
        return False
    recent = hist_df.tail(lookback)
    if "pct_chg" not in recent.columns:
        return False
    # 连续2天以上跌停（-9.5%以下视为跌停）
    limit_down = recent["pct_chg"] <= -9.5
    consecutive = limit_down.rolling(window=2).sum() >= 2
    return consecutive.any()


def run_pre_filter(
    stock_list: pd.DataFrame,
    delist_list: list[str] | None = None,
    min_trading_days: int = 60,
    min_daily_amount: float = 20_000_000,
) -> pd.DataFrame:
    """运行全部前置过滤，返回可通过的股票列表"""
    df = stock_list.copy()
    df = filter_st_stocks(df)
    df = filter_delisting(df, delist_list)
    df = filter_new_stocks(df, min_trading_days)
    logger.info(f"前置过滤完成: 最终 {len(df)} 只股票进入候选池")
    return df
