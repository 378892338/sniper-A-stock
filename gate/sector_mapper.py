"""
股票 → ETF分类指数映射（三层映射体系）

第一层: 申万行业 → ETF 硬映射
第二层: 概念板块 → ETF 概念补映射
第三层: 价格相关性兜底
"""

import pandas as pd
import numpy as np

from core.logger import get_logger

logger = get_logger("gate.sector_mapper")

# ETF分类指数体系
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
    "半导体":   {"code": "399678", "etf_name": "半导体ETF"},
}

# 第一层：申万行业 → ETF 硬映射
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

# 第二层：概念板块 → ETF 概念补映射
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


class SectorMapper:
    """股票到ETF分类指数的三层映射器"""

    def __init__(self):
        self.industry_to_etf = INDUSTRY_TO_ETF.copy()
        self.concept_to_etf = CONCEPT_TO_ETF.copy()
        self.etf_index_map = ETF_INDEX_MAP.copy()

    def map_stock_to_etf(self, symbol_industry: str = None,
                         symbol_concepts: list[str] = None,
                         price_corr_with_etfs: dict[str, float] = None
                         ) -> list[str]:
        """
        三层映射：股票 → ETF分类指数列表。

        返回: ETF名称列表 (如 ['芯片', '科技'])
        """
        etf_set = set()

        # 第一层：硬映射
        if symbol_industry and symbol_industry in self.industry_to_etf:
            for etf in self.industry_to_etf[symbol_industry]:
                etf_set.add(etf)
                # 已找到硬映射

        # 第二层：概念补映射
        if symbol_concepts:
            for concept in symbol_concepts:
                if concept in self.concept_to_etf:
                    for etf in self.concept_to_etf[concept]:
                        etf_set.add(etf)

        # 第三层：相关性兜底
        if not etf_set and price_corr_with_etfs:
            if price_corr_with_etfs:
                best_etf = max(price_corr_with_etfs, key=price_corr_with_etfs.get)
                corr = price_corr_with_etfs[best_etf]
                if corr > 0.6:  # 相关性阈值
                    etf_set.add(best_etf)
                    logger.info(f"相关性映射: -> {best_etf} (corr={corr:.3f})")

        return sorted(etf_set)

    def get_etf_code(self, etf_name: str) -> str | None:
        """获取ETF分类指数代码"""
        info = self.etf_index_map.get(etf_name)
        return info["code"] if info else None

    def get_all_etf_names(self) -> list[str]:
        return list(self.etf_index_map.keys())

    def add_industry_mapping(self, industry: str, etfs: list[str]):
        """动态添加行业映射"""
        self.industry_to_etf[industry] = etfs
        logger.info(f"新增行业映射: {industry} -> {etfs}")

    def add_concept_mapping(self, concept: str, etfs: list[str]):
        """动态添加概念映射"""
        self.concept_to_etf[concept] = etfs
        logger.info(f"新增概念映射: {concept} -> {etfs}")

    def remove_etf(self, etf_name: str):
        """ETF退市：移除所有相关映射"""
        self.etf_index_map.pop(etf_name, None)
        for industry, etfs in self.industry_to_etf.items():
            self.industry_to_etf[industry] = [e for e in etfs if e != etf_name]
        for concept, etfs in self.concept_to_etf.items():
            self.concept_to_etf[concept] = [e for e in etfs if e != etf_name]
        logger.info(f"ETF退市: {etf_name}")


def calc_price_correlation(stock_returns: pd.Series,
                           etf_returns: dict[str, pd.Series]) -> dict[str, float]:
    """
    计算个股与各ETF指数的价格相关性（用于第三层兜底映射）。

    stock_returns: 个股日收益率序列
    etf_returns: {etf_name: 日收益率序列}
    返回: {etf_name: correlation}
    """
    corr_map = {}
    for etf_name, etf_ret in etf_returns.items():
        if len(stock_returns) < 20 or len(etf_ret) < 20:
            continue
        aligned = pd.concat([stock_returns, etf_ret], axis=1).dropna()
        if len(aligned) < 20:
            continue
        corr = aligned.corr().iloc[0, 1]
        if not np.isnan(corr):
            corr_map[etf_name] = float(corr)
    return corr_map
