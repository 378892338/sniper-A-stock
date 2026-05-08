"""策略模块 — L0分类指数优先 + 四层筛子选股"""

import pandas as pd

from models.factors import calc_all_factors
from models.sector import SectorStrengthEngine
from config.settings import (
    STRUCTURE_CONFIG, TIMING_CONFIG, PIPE_WEIGHTS, CAPITAL_CONFIG, FUNDAMENTAL_CONFIG,
)


def rank_series(s: pd.Series) -> pd.Series:
    """百分位排名"""
    ranked = s.rank(pct=True, method="average")
    return ranked.fillna(0) * 100


class SieveStrategy:
    """L0分类指数优先 + 四层筛子策略"""

    def __init__(self, sector_weight: float = 0.40):
        self.structure_cfg = STRUCTURE_CONFIG
        self.timing_cfg = TIMING_CONFIG
        self.capital_cfg = CAPITAL_CONFIG
        self.fundamental_cfg = FUNDAMENTAL_CONFIG
        self.pipe_weights = PIPE_WEIGHTS
        self.sector_weight = sector_weight
        self.sector_engine = SectorStrengthEngine(sector_weight)

    def init_sectors(self, start: str = "20190101", end: str = "20260430"):
        """初始化分类指数数据（启动时调用一次）"""
        self.sector_engine.load_data(start, end)

    def score_single(
        self,
        df_daily: pd.DataFrame,
        df_weekly: pd.DataFrame = None,
        market_ret: pd.Series = None,
        symbol: str = "",
    ) -> pd.Series:
        """
        单只股票因子评分（不含L0分类指数加权）。
        委托 calc_all_factors 计算原始因子值，再用 rank_series 做截面标准化。
        """
        result = calc_all_factors(df_daily, df_weekly, market_ret, symbol=symbol)
        tech = rank_series(result["score_technical"]) * self.pipe_weights["technical"]
        cap = rank_series(result["score_capital"]) * self.pipe_weights["capital"]
        fund = rank_series(result["score_fundamental"]) * self.pipe_weights["fundamental"]
        total = tech + cap + fund
        return rank_series(total)

    def score_with_sector(
        self,
        symbol: str,
        df_daily: pd.DataFrame,
        date: str,
        df_weekly: pd.DataFrame = None,
        market_ret: pd.Series = None,
    ) -> dict:
        """
        单只股票 L0+因子 综合评分。
        返回 {factor_score, final_score, sector_boost, sector_avg, best_sector}
        """
        scores = self.score_single(df_daily, df_weekly, market_ret, symbol=symbol)
        factor_score = float(scores.loc[date]) if date in scores.index else 50.0

        result = self.sector_engine.get_stock_score_with_sector(
            stock_symbol=symbol,
            stock_score=factor_score,
            date=date,
            stock_df=df_daily,
        )
        result['factor_score'] = factor_score
        return result

    def neutralize_factors(
        self,
        raw_scores: dict[str, float],
        market_caps: dict[str, float] = None,
        sectors: dict[str, str] = None,
    ) -> dict[str, float]:
        """市值和行业中性化：对原始评分做横截面回归取残差"""
        syms = list(raw_scores.keys())
        if len(syms) < 20 or market_caps is None or sectors is None:
            return raw_scores

        import numpy as np
        y = np.array([raw_scores[s] for s in syms])

        # 构建设计矩阵: log市值 + 行业虚拟变量
        X_list = [np.ones(len(syms))]
        caps = np.array([np.log(market_caps.get(s, np.nan)) for s in syms], dtype=float)
        cap_mask = ~np.isnan(caps) & ~np.isinf(caps)
        if cap_mask.sum() > 10:
            caps[~cap_mask] = np.nanmean(caps[cap_mask])
            X_list.append(caps)

        # 行业哑变量 (只保留股票数>=5的行业)
        sector_counts = {}
        for s in syms:
            sec = sectors.get(s, "other")
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        valid_sectors = {k for k, v in sector_counts.items() if v >= 5}
        for sec in sorted(valid_sectors):
            X_list.append(np.array([1.0 if sectors.get(s, "") == sec else 0.0 for s in syms]))

        X = np.column_stack(X_list)

        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            residuals = y - X @ beta
            return {s: float(residuals[i]) for i, s in enumerate(syms)}
        except Exception:
            return raw_scores

    def select_top(
        self,
        scores: dict[str, pd.Series],
        date: str,
        top_n: int = 30,
        market_caps: dict[str, float] = None,
        sectors: dict[str, str] = None,
    ) -> list[str]:
        """在给定日期选Top N（可选市值/行业中性化）"""
        day_scores = {}
        for sym, s in scores.items():
            if date in s.index:
                day_scores[sym] = s.loc[date]

        if not day_scores:
            return []

        if market_caps is not None and sectors is not None:
            day_scores = self.neutralize_factors(day_scores, market_caps, sectors)

        ranked = sorted(day_scores.items(), key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in ranked[:top_n]]

    def daily_report_data(
        self,
        stock_data: dict[str, pd.DataFrame],
        date: str,
    ) -> dict:
        """
        生成每日报告数据：
        - 分类指数强势Top5
        - 每个指数下个股Top3
        - 跨指数检测结果
        """
        # 1. 分类指数排序
        top_sectors = self.sector_engine.get_top_sectors(date, top_n=5)

        # 2. 所有个股评分
        all_stock_scores = {}
        for sym, df in stock_data.items():
            if date not in df.index:
                continue
            df_before = df[df.index <= date]
            if len(df_before) < 200:
                continue
            result = self.score_with_sector(sym, df_before, date)
            all_stock_scores[sym] = result

        # 3. 按分类指数分组
        from models.sector import SW_INDEX_MAP, BROAD_INDEX_MAP, assign_stock_sectors
        all_sectors = {**SW_INDEX_MAP, **BROAD_INDEX_MAP}

        sector_stocks = {}
        for sym, info in all_stock_scores.items():
            bs = info.get('best_sector', '000905')
            if bs not in sector_stocks:
                sector_stocks[bs] = []
            sector_stocks[bs].append((sym, info))

        # 每个指数取Top3
        sector_top3 = {}
        for code, stocks in sector_stocks.items():
            stocks.sort(key=lambda x: x[1]['final_score'], reverse=True)
            sector_top3[code] = {
                'name': all_sectors.get(code, code),
                'stocks': [(s, {
                    'final_score': i['final_score'],
                    'factor_score': i['factor_score'],
                    'sector_boost': i['sector_boost'],
                }) for s, i in stocks[:3]],
            }

        return {
            'date': date,
            'top_sectors': top_sectors,
            'sector_top3': sector_top3,
            'all_scores': all_stock_scores,
        }
