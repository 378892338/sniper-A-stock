"""ETF分类指数数据获取"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from data.sources import get_source
from gate.sector_mapper import ETF_INDEX_MAP
from shared.cache import read_cache, write_cache
from shared.retry import retry
from core.logger import get_logger

logger = get_logger("data.index_etf")


def fetch_etf_index_data(etf_name: str, start: str, end: str,
                         source: str = "akshare") -> pd.DataFrame:
    """获取单个ETF分类指数的日线数据"""
    info = ETF_INDEX_MAP.get(etf_name)
    if info is None:
        logger.warning(f"未知ETF指数: {etf_name}")
        return pd.DataFrame()

    code = info["code"]
    cache_key = f"etf_index_{code}_{start}_{end}"
    cached = read_cache(cache_key, ttl_seconds=3600)
    if cached is not None and not cached.empty:
        return cached

    ds = get_source()
    try:
        df = ds.fetch_index_daily(code, start, end)
        if not df.empty:
            df["etf_name"] = etf_name
            write_cache(df, cache_key)
            logger.info(f"ETF指数 {etf_name}({code}): {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"获取ETF指数 {etf_name}({code}) 失败: {e}")
        return pd.DataFrame()


def fetch_all_etf_indices(start: str, end: str, etf_names: list[str] = None,
                          sleep: float = 0.2) -> dict[str, pd.DataFrame]:
    """批量获取所有ETF分类指数数据"""
    if etf_names is None:
        etf_names = list(ETF_INDEX_MAP.keys())

    result: dict[str, pd.DataFrame] = {}
    max_workers = min(8, len(etf_names))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_name = {
            executor.submit(fetch_etf_index_data, name, start, end): name
            for name in etf_names
        }
        for i, future in enumerate(as_completed(future_to_name)):
            name = future_to_name[future]
            try:
                df = future.result(timeout=60)
                if df is not None and not df.empty:
                    result[name] = df
            except Exception as e:
                logger.error(f"获取ETF {name} 失败: {e}")
            if (i + 1) % 5 == 0:
                logger.info(f"ETF指数: {i+1}/{len(etf_names)}")

    logger.info(f"ETF指数获取完成: {len(result)}/{len(etf_names)}")
    return result


def get_etf_stocks(etf_name: str, source: str = "akshare") -> list[str]:
    """获取ETF分类指数对应的成分股列表"""
    from gate.sector_mapper import INDUSTRY_TO_ETF, CONCEPT_TO_ETF
    from data.industry import fetch_industry_members, fetch_concept_members

    etf_industries = [ind for ind, etfs in INDUSTRY_TO_ETF.items() if etf_name in etfs]
    etf_concepts = [con for con, etfs in CONCEPT_TO_ETF.items() if etf_name in etfs]

    stocks: set[str] = set()

    if etf_industries:
        industry_df = fetch_industry_members(source=source)
        if not industry_df.empty and "symbol" in industry_df.columns and "industry" in industry_df.columns:
            matching = industry_df[industry_df["industry"].isin(etf_industries)]
            stocks.update(matching["symbol"].tolist())

    if etf_concepts:
        concept_df = fetch_concept_members(source=source)
        if not concept_df.empty and "symbol" in concept_df.columns and "concept" in concept_df.columns:
            matching = concept_df[concept_df["concept"].isin(etf_concepts)]
            stocks.update(matching["symbol"].tolist())

    logger.info(f"ETF {etf_name}: 成分股 {len(stocks)} 只")
    return sorted(stocks)
