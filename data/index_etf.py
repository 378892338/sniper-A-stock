"""ETF分类指数数据获取"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from data.sources import get_source
from shared.cache import read_cache, write_cache
from shared.retry import retry
from core.logger import get_logger

logger = get_logger("data.index_etf")

# ETF 分类指数体系
ETF_INDEX_MAP = {
    "证券":     {"code": "399975", "etf_name": "证券ETF"},
    "银行":     {"code": "399986", "etf_name": "银行ETF"},
    "军工":     {"code": "399967", "etf_name": "军工ETF"},
    "芯片":     {"code": "990001", "etf_name": "芯片ETF"},
    "半导体":   {"code": "399678", "etf_name": "半导体ETF"},
    "新能源车": {"code": "399976", "etf_name": "新能源车ETF"},
    "光伏":     {"code": "399395", "etf_name": "光伏ETF"},
    "消费":     {"code": "000932", "etf_name": "消费ETF"},
    "医药":     {"code": "000933", "etf_name": "医药ETF"},
    "酒":       {"code": "399997", "etf_name": "酒ETF"},
    "科技":     {"code": "399440", "etf_name": "科技ETF"},
    "有色":     {"code": "000819", "etf_name": "有色ETF"},
    "煤炭":     {"code": "399998", "etf_name": "煤炭ETF"},
    "汽车":     {"code": "399432", "etf_name": "汽车ETF"},
}

# 申万行业 → ETF 硬映射
INDUSTRY_TO_ETF = {
    "医药生物":   ["医药"],
    "银行":       ["银行"],
    "食品饮料":   ["消费", "酒"],
    "电子":       ["芯片"],
    "计算机":     ["科技"],
    "国防军工":   ["军工"],
    "汽车":       ["汽车", "新能源车"],
    "非银金融":   ["证券"],
    "有色金属":   ["有色"],
    "煤炭":       ["煤炭"],
    "电力设备":   ["光伏", "新能源车"],
    "家用电器":   ["消费"],
    "农林牧渔":   ["消费"],
    "纺织服饰":   ["消费"],
    "轻工制造":   ["消费"],
    "商贸零售":   ["消费"],
    "社会服务":   ["消费"],
    "传媒":       ["科技"],
    "通信":       ["科技"],
    "机械设备":   ["新能源车"],
    "基础化工":   ["新能源车"],
    "钢铁":       ["有色"],
    "石油石化":   ["煤炭"],
    "公用事业":   ["新能源车"],
    "建筑装饰":   ["证券"],
    "房地产":     ["证券"],
}

# 概念板块 → ETF 概念补映射
CONCEPT_TO_ETF = {
    "光刻胶":    ["半导体"],
    "CRO":       ["医药"],
    "HJT电池":   ["光伏"],
    "TOPCon":    ["光伏"],
    "固态电池":  ["新能源车"],
    "钠电池":    ["新能源车"],
    "数据要素":  ["科技"],
    "信创":      ["科技"],
    "ChatGPT":   ["科技"],
    "AIGC":      ["科技"],
    "CPO":       ["芯片"],
    "先进封装":  ["半导体"],
    "存储芯片":  ["芯片"],
    "无人驾驶":  ["汽车"],
    "一体化压铸": ["汽车"],
    "白酒":      ["酒"],
    "医美":      ["医药"],
    "创新药":    ["医药"],
    "中药":      ["医药"],
    "稀土永磁":  ["有色"],
    "锂矿":      ["有色"],
    "光伏建筑一体化": ["光伏"],
    "虚拟电厂":  ["新能源车"],
    "充电桩":    ["新能源车"],
}


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
