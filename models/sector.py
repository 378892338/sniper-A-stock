"""分类指数模块 — L0层（最优先）

申万行业指数强弱评分 + 个股行业归属 + 跨指数自适应
"""

import numpy as np
import pandas as pd
import akshare as ak
from scipy.stats import spearmanr

from core.logger import get_logger
logger = get_logger("models.sector")


# ====== 申万一级行业指数代码映射 ======
SW_INDEX_MAP = {
    "801010": "农林牧渔", "801020": "采掘", "801030": "化工",
    "801040": "钢铁", "801050": "有色金属", "801080": "电子",
    "801110": "家用电器", "801120": "食品饮料", "801130": "纺织服装",
    "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业",
    "801170": "交通运输", "801180": "房地产", "801200": "商业贸易",
    "801210": "休闲服务", "801230": "综合", "801710": "建筑材料",
    "801720": "建筑装饰", "801730": "电气设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信",
    "801780": "银行", "801790": "非银金融", "801880": "汽车",
    "801890": "机械设备",
}

# 跨指数分类（个股可能属于多个指数）
BROAD_INDEX_MAP = {
    "000016": "上证50", "000300": "沪深300", "000905": "中证500",
    "399006": "创业板指", "000688": "科创50", "000852": "中证1000",
}


def fetch_sector_indices(start: str = "20190101", end: str = "20260430") -> dict[str, pd.DataFrame]:
    """获取申万行业指数日线"""
    result = {}
    for i, (code, name) in enumerate(SW_INDEX_MAP.items()):
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=f"sh{code}",
                start_date=start,
                end_date=end,
            )
            if len(df) < 100:
                continue
            df = df.rename(columns={
                'date': 'date', 'open': 'open', 'close': 'close',
                'high': 'high', 'low': 'low',
            })
            df['date'] = pd.to_datetime(df['date'])
            df['volume'] = df.get('amount', 0) / df['close'] * 100
            df = df.set_index('date')
            df['name'] = name
            result[code] = df
        except Exception as e:
            logger.debug(f"申万指数 {code}({name}) 获取失败: {e}")
    return result


def fetch_broad_indices(start: str = "20190101", end: str = "20260430") -> dict[str, pd.DataFrame]:
    """获取宽基指数日线（上证50/沪深300/中证500/创业板指/科创50/中证1000）"""
    result = {}
    for code, name in BROAD_INDEX_MAP.items():
        try:
            market = "sz" if code.startswith("3") else "sh"
            df = ak.stock_zh_a_hist_tx(
                symbol=f"{market}{code}",
                start_date=start,
                end_date=end,
            )
            if len(df) < 100:
                continue
            df = df.rename(columns={
                'date': 'date', 'open': 'open', 'close': 'close',
                'high': 'high', 'low': 'low',
            })
            df['date'] = pd.to_datetime(df['date'])
            df['volume'] = df.get('amount', 0) / df['close'] * 100
            df = df.set_index('date')
            df['name'] = name
            result[code] = df
        except Exception as e:
            logger.debug(f"宽基指数 {code}({name}) 获取失败: {e}")
    return result


def calc_sector_strength(df: pd.DataFrame) -> pd.Series:
    """
    计算分类指数强弱评分 (0-100)
    综合：趋势强度 + 动量 + 相对大盘alpha + 量能
    """
    close = df['close']
    result = pd.DataFrame(index=df.index)

    # 1. 趋势强度：MA排列得分
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    result['trend'] = ((ma5 > ma20).astype(float) * 15 +
                       (ma20 > ma60).astype(float) * 15 +
                       (close > ma60).astype(float) * 10)

    # 2. 动量：N日涨跌幅排名
    for n, w in [(5, 10), (10, 10), (20, 15), (60, 10)]:
        ret = close.pct_change(n)
        result[f'mom_{n}'] = ret.rank(pct=True) * w

    # 3. 波动率惩罚：低波动=稳=高分
    vol = close.pct_change().rolling(20).std()
    result['vol_score'] = (1 - vol.rank(pct=True)) * 10

    # 4. 量能趋势
    if 'volume' in df.columns:
        vol_ma = df['volume'].rolling(20).mean()
        vol_ratio = df['volume'] / vol_ma
        result['vol_trend'] = ((vol_ratio > 1).astype(float) * 5 +
                               (vol_ratio > 1.3).astype(float) * 5)

    total = result.sum(axis=1)
    # 标准化到0-100
    total = (total - total.rolling(250).min()) / (total.rolling(250).max() - total.rolling(250).min() + 1e-9) * 100
    return total.fillna(50)


def assign_stock_sectors(symbol: str) -> list[str]:
    """根据股票代码推断所属行业分类"""
    # 创业板
    if symbol.startswith("3"):
        sectors = ["399006"]  # 创业板指
        if symbol.startswith("300"):
            return sectors  # 创业板
    # 科创板
    if symbol.startswith("688"):
        return ["000688"]  # 科创50
    # 主板
    if symbol.startswith("60") or symbol.startswith("00"):
        return ["000300", "000905"]  # 可能属于沪深300或中证500

    return ["000905"]  # 默认中证500


def calc_cross_index_following(
    symbol: str,
    stock_df: pd.DataFrame,
    index_dfs: dict[str, pd.DataFrame],
    lookback: int = 120,
) -> dict[str, float]:
    """
    跨指数自适应：检测个股更贴近哪个指数
    计算个股与各指数的相关系数，返回 {指数代码: 相关度}
    """
    stock_ret = stock_df['close'].pct_change().dropna()
    if len(stock_ret) < lookback:
        lookback = len(stock_ret)

    stock_recent = stock_ret.iloc[-lookback:]

    correlations = {}
    for code, idf in index_dfs.items():
        if len(idf) < lookback:
            continue
        idx_ret = idf['close'].pct_change().dropna().iloc[-lookback:]

        # 对齐日期
        common_idx = stock_recent.index.intersection(idx_ret.index)
        if len(common_idx) < 30:
            continue

        corr = stock_ret.loc[common_idx].corr(idx_ret.loc[common_idx])
        correlations[code] = corr

    return correlations


def calc_index_weighted_score(
    stock_score: float,
    sector_scores: dict[str, float],
    stock_sectors: list[str],
    sector_weight: float = 0.40,
) -> float:
    """
    L0层加权：分类指数优先
    final = sector_weight * avg(sector_scores) + (1-sector_weight) * stock_score
    """
    if not stock_sectors:
        return stock_score

    relevant = [sector_scores.get(s, 50) for s in stock_sectors]
    if not relevant:
        return stock_score

    avg_sector = np.mean(relevant)
    return sector_weight * avg_sector + (1 - sector_weight) * stock_score


class SectorStrengthEngine:
    """分类指数引擎"""

    def __init__(self, sector_weight: float = 0.40):
        self.sector_weight = sector_weight
        self.sector_data: dict[str, pd.DataFrame] = {}
        self.broad_data: dict[str, pd.DataFrame] = {}
        self.sector_strength: dict[str, pd.Series] = {}
        self.cross_index_cache: dict[str, dict[str, float]] = {}

    def load_data(self, start: str = "20190101", end: str = "20260430"):
        """加载分类指数数据"""
        print("  加载申万行业指数...")
        self.sector_data = fetch_sector_indices(start, end)
        print(f"    申万行业: {len(self.sector_data)} 个")

        print("  加载宽基指数...")
        self.broad_data = fetch_broad_indices(start, end)
        print(f"    宽基指数: {len(self.broad_data)} 个")

        all_data = {**self.sector_data, **self.broad_data}

        print("  计算指数强弱...")
        for code, df in all_data.items():
            self.sector_strength[code] = calc_sector_strength(df)

        print(f"  指数引擎就绪: {len(self.sector_strength)} 个指数")

    def get_sector_strength_at(self, date: str) -> dict[str, float]:
        """获取某日所有指数强弱评分"""
        result = {}
        for code, ss in self.sector_strength.items():
            if date in ss.index:
                result[code] = float(ss.loc[date])
        return result

    def get_top_sectors(self, date: str, top_n: int = 5) -> list[tuple[str, str, float]]:
        """获取某日最强势的分类指数Top N，返回[(code, name, score)]"""
        sector_names = {**SW_INDEX_MAP, **BROAD_INDEX_MAP}
        scores = self.get_sector_strength_at(date)
        scored = []
        for code, score in scores.items():
            name = sector_names.get(code, code)
            scored.append((code, name, score))
        scored.sort(key=lambda x: x[2], reverse=True)
        return scored[:top_n]

    def get_stock_score_with_sector(
        self,
        stock_symbol: str,
        stock_score: float,
        date: str,
        stock_df: pd.DataFrame = None,
    ) -> dict:
        """
        计算个股的L0分类指数加权后分数
        返回 {final_score, sector_boost, best_sector, ...}
        """
        # 所属分类指数
        sectors = assign_stock_sectors(stock_symbol)

        # 跨指数检测
        if stock_df is not None and len(sectors) > 1:
            cache_key = f"{stock_symbol}"
            if cache_key not in self.cross_index_cache:
                self.cross_index_cache[cache_key] = calc_cross_index_following(
                    stock_symbol, stock_df, {**self.sector_data, **self.broad_data}
                )
            corrs = self.cross_index_cache[cache_key]
            if corrs:
                # 按相关度排序，取最贴合的指数
                best = max(corrs, key=corrs.get)
                sectors = [best] + [s for s in sectors if s != best]

        # 取相关指数的当前强度
        sector_scores_at_date = self.get_sector_strength_at(date)
        relevant = {}
        for s in sectors:
            if s in sector_scores_at_date:
                relevant[s] = sector_scores_at_date[s]

        if not relevant:
            return {
                'final_score': stock_score,
                'sector_boost': 0,
                'best_sector': None,
                'sector_avg': 50,
            }

        avg_sector = np.mean(list(relevant.values()))
        final = self.sector_weight * avg_sector + (1 - self.sector_weight) * stock_score

        return {
            'final_score': final,
            'sector_boost': final - stock_score,
            'best_sector': max(relevant, key=relevant.get),
            'sector_avg': avg_sector,
            'sector_details': relevant,
        }
