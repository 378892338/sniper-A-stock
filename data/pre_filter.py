"""前置过滤器 — ST/退市/流动性/次新股排除"""

import pandas as pd

from core.logger import get_logger

logger = get_logger("data.pre_filter")


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


def filter_frozen(df: pd.DataFrame, frozen_list: list[str] | None = None) -> pd.DataFrame:
    """过滤处于 FROZEN（停牌）状态的股票。

    frozen_list 由数据加载器生成：连续 N 个交易日成交量为 0 的股票视为停牌。
    如果未提供停牌列表，则尝试从 df 的 frozen/suspended 列推断。
    """
    if frozen_list is None:
        # 尝试从 df 列推断
        for col in ["frozen", "suspended", "status"]:
            if col in df.columns:
                frozen_list = df[df[col] == "frozen"]["symbol"].tolist()
                break
        if not frozen_list:
            return df

    before = len(df)
    sym_col = None
    for col in ["symbol", "代码"]:
        if col in df.columns:
            sym_col = col
            break
    if sym_col is None:
        return df

    result = df[~df[sym_col].isin(frozen_list)]
    if before != len(result):
        logger.info(f"FROZEN过滤: {before - len(result)} 只停牌股票排除")
    return result


def mark_frozen(df: pd.DataFrame, volume_col: str = "volume", threshold_days: int = 5) -> pd.DataFrame:
    """标记处于停牌状态的股票（连续 N 天成交量为 0）。

    返回添加了 'frozen' 标记列的股票列表。
    frozen_list 可在后续过滤中使用。
    """
    from data.local.warehouse import LocalDataWarehouse

    wh = LocalDataWarehouse()
    frozen: list[str] = []

    for _, row in df.head(2000).iterrows():  # 性能：最多检查前2000只
        sym = row.get("symbol") or row.get("代码")
        if not sym:
            continue
        bars = wh.get_daily_bars(str(sym), start="2000-01-01", end="2099-12-31")
        if bars.empty:
            continue
        if volume_col not in bars.columns:
            continue

        vol = bars[volume_col]
        zero_runs = 0
        max_run = 0
        for v in vol:
            if v == 0 or (isinstance(v, float) and pd.isna(v)):
                zero_runs += 1
                max_run = max(max_run, zero_runs)
            else:
                zero_runs = 0
        if max_run >= threshold_days:
            frozen.append(str(sym))

    if frozen:
        logger.info(f"检测到 {len(frozen)} 只股票连续{threshold_days}天成交量为0: {frozen[:10]}...")
    else:
        logger.info("未检测到停牌股票")

    return df


def run_pre_filter(
    stock_list: pd.DataFrame,
    min_trading_days: int = 60,
    frozen_list: list[str] | None = None,
) -> pd.DataFrame:
    """运行全部前置过滤，返回可通过的股票列表

    Args:
        stock_list: 待过滤的股票列表
        min_trading_days: 最小交易天数（次新股过滤）
        frozen_list: 停牌股票代码列表（可由 mark_frozen 生成）
    """
    df = stock_list.copy()
    df = filter_st_stocks(df)
    df = filter_new_stocks(df, min_trading_days)
    df = filter_frozen(df, frozen_list)
    logger.info(f"前置过滤完成: 最终 {len(df)} 只股票进入候选池")
    return df
