"""行业分类数据 — 申万行业 + 概念板块（供 sector_mapper 使用）"""

import pandas as pd

from data.sources import get_source
from shared.cache import read_cache, write_cache
from core.logger import get_logger

logger = get_logger("data.industry")

# 申万行业缓存 key
INDUSTRY_CACHE_KEY = "sw_industry_cons"
# 概念板块缓存 key
CONCEPT_CACHE_KEY = "sw_concept_cons"


def fetch_industry_members(source: str = "akshare") -> pd.DataFrame:
    """
    获取全A股申万行业分类。

    返回: DataFrame with columns [symbol, name, industry]
    """
    cached = read_cache(INDUSTRY_CACHE_KEY, ttl_seconds=86400)  # 24h TTL
    if cached is not None and not cached.empty:
        return cached

    ds = get_source(source)
    if not ds.is_available():
        logger.warning(f"数据源 {ds.name()} 不可用，无法获取行业分类")
        return pd.DataFrame()

    try:
        df = ds.fetch_industry_members()
        if df is None or df.empty:
            logger.warning(f"数据源 {ds.name()} 不支持行业分类数据")
            return pd.DataFrame()
        logger.info(f"申万行业分类: {len(df)} 条")
        write_cache(df, INDUSTRY_CACHE_KEY)
        return df
    except Exception as e:
        logger.error(f"获取申万行业分类失败: {e}")
        return pd.DataFrame()


def fetch_concept_members(source: str = "akshare") -> pd.DataFrame:
    """
    获取全A股概念板块分类。

    返回: DataFrame with columns [symbol, name, concept]
    """
    cached = read_cache(CONCEPT_CACHE_KEY, ttl_seconds=86400)
    if cached is not None and not cached.empty:
        return cached

    ds = get_source(source)
    if not ds.is_available():
        logger.warning(f"数据源 {ds.name()} 不可用，无法获取概念板块")
        return pd.DataFrame()

    try:
        df = ds.fetch_concept_members()
        if df is None or df.empty:
            logger.warning(f"数据源 {ds.name()} 不支持概念板块数据")
            return pd.DataFrame()
        logger.info(f"概念板块分类: {len(df)} 条")
        write_cache(df, CONCEPT_CACHE_KEY)
        return df
    except Exception as e:
        logger.error(f"获取概念板块分类失败: {e}")
        return pd.DataFrame()


def build_symbol_industry_map(industry_df: pd.DataFrame | None = None
                              ) -> dict[str, str]:
    """
    构建 股票代码 → 申万行业 映射。

    返回: {symbol: industry_name}
    """
    if industry_df is None:
        industry_df = fetch_industry_members()
    if industry_df.empty:
        return {}

    if "symbol" not in industry_df.columns or "industry" not in industry_df.columns:
        logger.warning("行业数据缺少必要列")
        return {}

    return dict(zip(industry_df["symbol"], industry_df["industry"]))


def build_symbol_concepts_map(concept_df: pd.DataFrame | None = None
                              ) -> dict[str, list[str]]:
    """
    构建 股票代码 → 概念板块列表 映射。

    返回: {symbol: [concept1, concept2, ...]}
    """
    if concept_df is None:
        concept_df = fetch_concept_members()
    if concept_df.empty:
        return {}

    if "symbol" not in concept_df.columns or "concept" not in concept_df.columns:
        logger.warning("概念数据缺少必要列")
        return {}

    result: dict[str, list[str]] = {}
    for _, row in concept_df.iterrows():
        sym = row["symbol"]
        concept = row["concept"]
        if sym not in result:
            result[sym] = []
        result[sym].append(concept)

    return result


def get_symbol_classification(symbol: str,
                              industry_map: dict[str, str] | None = None,
                              concept_map: dict[str, list[str]] | None = None
                              ) -> tuple[str | None, list[str]]:
    """
    获取单个股票的行业和概念分类。

    返回: (industry_name, [concept_names])
    """
    if industry_map is None:
        industry_map = build_symbol_industry_map()
    if concept_map is None:
        concept_map = build_symbol_concepts_map()

    industry = industry_map.get(symbol)
    concepts = concept_map.get(symbol, [])
    return industry, concepts
