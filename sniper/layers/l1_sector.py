"""L1 板块评分 — 4 维度板块排名 + Beta 中性化"""

import numpy as np
import pandas as pd

from sniper.config import SECTOR as CFG
from sniper.data_router import DataRouter
from core.logger import get_logger

logger = get_logger("sniper.layers.l1_sector")


class SectorScorer:
    """板块评分器。每日对申万行业进行 4 维评分，输出 Top N 板块。"""

    def __init__(self, router: DataRouter | None = None):
        self.router = router or DataRouter()

    def score_momentum(self, date: str) -> dict[str, float]:
        """动量维度：近 N 日涨幅。"""
        df = self.router.get_industry_compare_range("1900-01-01", date)
        if df.empty:
            return {}
        recent = df[df["date"] <= date].tail(50)
        if recent.empty:
            return {}
        # 取每板块最近 CFG.momentum_window 日涨幅均值
        recent = recent.sort_values("date")
        recent["cum_ret"] = recent.groupby("industry_name")["daily_change"].transform(
            lambda x: x.tail(CFG.momentum_window).sum()
        )
        latest = recent[recent["date"] == recent["date"].max()]
        return dict(zip(latest["industry_name"], latest["cum_ret"]))

    def score_fund_flow(self, date: str) -> dict[str, float]:
        """资金维度：用量能变化代理资金流向。

        优先从 industry_compare 的 volume_ratio（MA20 基准）读取，
        回退到旧版 volume_change（sw_index_daily 数据源兼容）。
        """
        df = self.router.get_industry_compare(date)
        if df.empty:
            return {}

        result = {}
        if "volume_ratio" in df.columns:
            # 新数据：volume_ratio（行业成交量/MA20），范围 0.1~5.0
            for _, row in df.iterrows():
                vr = row.get("volume_ratio", 1.0) or 1.0
                # 0.3→0, 1.0→58, 1.5→100
                score = max(0, min(100, (vr - 0.3) / 1.2 * 100))
                result[row["industry_name"]] = round(score, 1)
        else:
            # 旧数据：volume_change，范围 -100~+100
            vol_col = next((c for c in ["volume_change", "成交额变化", "vol_change"] if c in df.columns), None)
            if vol_col is None:
                logger.debug("行业对比表无量能列，回退等分")
                return {row["industry_name"]: 50.0 for _, row in df.iterrows()}
            for _, row in df.iterrows():
                vc = row.get(vol_col, 0) or 0
                score = max(0, min(100, 50 + vc * 25))
                result[row["industry_name"]] = round(score, 1)
        return result

    def score_breadth(self, date: str) -> dict[str, float]:
        """广度维度：板块内上涨个股占比。

        优先从 industry_compare 的 breadth 列读取（从 daily_bars 计算，实时准确），
        回退到强势股表标签统计。
        """
        # 优先：industry_compare.breadth 列
        ind_df = self.router.get_industry_compare(date)
        if "breadth" in ind_df.columns and not ind_df.empty:
            non_null = ind_df[ind_df["breadth"].notna()]
            if not non_null.empty:
                return dict(zip(non_null["industry_name"], non_null["breadth"].round(1)))

        # 回退：强势股表标签统计
        dt = self.router.get_hot_stocks(date)
        if dt.empty:
            industry_df = self.router.get_industry_compare(date)
            if industry_df.empty:
                return {}
            sc_col = None
            for col in ["stock_count", "成分股数", "stock_cnt"]:
                if col in industry_df.columns:
                    sc_col = col
                    break
            if sc_col is not None:
                max_count = industry_df[sc_col].max() or 1
                return {
                    row["industry_name"]: max(0, min(100, row[sc_col] / max_count * 100))
                    for _, row in industry_df.iterrows()
                }
            return {row["industry_name"]: 50.0 for _, row in industry_df.iterrows()}

        # 统计每个板块的强势股数量
        stock_counts: dict[str, int] = {}
        for _, row in dt.iterrows():
            # reason_tags 可能包含行业信息
            tags = str(row.get("reason_tags", ""))
            if tags:
                for tag in tags.split(";"):
                    tag = tag.strip()
                    if tag:
                        if tag not in stock_counts:
                            stock_counts[tag] = 0
                        stock_counts[tag] += 1

        if not stock_counts:
            # 无行业标签，回退到等分
            return {}

        max_count = max(stock_counts.values()) or 1
        return {
            ind: max(0, min(100, count / max_count * 100))
            for ind, count in stock_counts.items()
        }

    def score_heat(self, date: str) -> dict[str, float]:
        """热点维度：题材/强势股覆盖度。"""
        df = self.router.get_industry_compare(date)
        if df.empty:
            return {}
        # 用涨幅排名逆映射热度
        max_rank = df["rank"].max() or 1
        return {
            row["industry_name"]: (1 - (row["rank"] - 1) / max_rank) * 100
            for _, row in df.iterrows()
        }

    def composite_scores(self, date: str) -> pd.DataFrame:
        """合成 4 维评分，返回排名后的 DataFrame。

        加入 Beta 中性化：将板块评分减去 Beta×大盘收益，
        消除牛市/熊市整体涨跌对板块区分度的影响。
        """
        momentum = self.score_momentum(date)
        fund_flow = self.score_fund_flow(date)
        breadth = self.score_breadth(date)
        heat = self.score_heat(date)

        all_industries = set(momentum) | set(fund_flow) | set(breadth) | set(heat)
        if not all_industries:
            return pd.DataFrame()

        rows = []
        for ind in all_industries:
            m = self._normalize(momentum.get(ind, 0), momentum.values())
            f = fund_flow.get(ind, 50.0)
            b = breadth.get(ind, 50.0)
            h = heat.get(ind, 50.0)
            total = (
                m * CFG.momentum_weight
                + f * CFG.fund_flow_weight
                + b * CFG.breadth_weight
                + h * CFG.heat_weight
            )
            rows.append({
                "industry_name": ind,
                "momentum": round(m, 1),
                "fund_flow": round(f, 1),
                "breadth": round(b, 1),
                "heat": round(h, 1),
                "composite": round(total, 1),
            })

        out = pd.DataFrame(rows)
        if out.empty:
            return out

        # ── Beta 中性化 ──
        out["composite"] = self._beta_neutralize(out["industry_name"], out["composite"])

        out = out.sort_values("composite", ascending=False).reset_index(drop=True)
        out["rank"] = range(1, len(out) + 1)
        return out

    def top_sectors(self, date: str, top_n: int | None = None) -> list[str]:
        """返回 Top N 板块名称列表。

        Args:
            top_n: 指定选取板块数。为 None 时使用 CFG.top_n。
        """
        df = self.composite_scores(date)
        if df.empty:
            return []
        n = top_n if top_n is not None else CFG.top_n
        top = df.head(n)["industry_name"].tolist()
        logger.info(f"L1 {date}: Top {n} 板块 = {top}")
        return top

    @staticmethod
    def _normalize(val: float, values: list) -> float:
        if not values:
            return 50.0
        mn, mx = min(values), max(values)
        if mx - mn == 0:
            return 50.0
        return (val - mn) / (mx - mn) * 100

    _benchmark_cache: dict = None
    _beta_cache: dict[str, float] = None

    def _get_benchmark_returns(self) -> pd.Series:
        """获取基准（沪深300）日收益率序列，缓存避免重复查询。"""
        if self._benchmark_cache is None:
            try:
                bm = self.router.get_index_daily("csi300")
                if not bm.empty and "close" in bm.columns:
                    bm = bm.sort_values("date")
                    self._benchmark_cache = bm["close"].pct_change().fillna(0)
                else:
                    self._benchmark_cache = pd.Series(dtype=float)
            except Exception as e:
                logger.debug(f"获取基准收益率失败: {e}")
                self._benchmark_cache = pd.Series(dtype=float)
        return self._benchmark_cache

    def _compute_betas(self, sector_returns: dict[str, pd.Series]) -> dict[str, float]:
        """横截面计算每个板块对沪深300的beta值。

        用最近60个交易日做线性回归: sector_ret = alpha + beta * market_ret + error
        """
        mkt_ret = self._get_benchmark_returns()
        if mkt_ret is None or mkt_ret.empty or len(mkt_ret) < 60:
            return {}

        recent_mkt = mkt_ret.tail(60).values

        betas: dict[str, float] = {}
        for sector_name, sector_series in sector_returns.items():
            if sector_series is None or sector_series.empty:
                betas[sector_name] = 1.0
                continue

            # 对齐日期
            recent_sector = sector_series.tail(60).values
            if len(recent_sector) < 60:
                betas[sector_name] = 1.0
                continue

            # 简单线性回归: beta = cov(s, m) / var(m)
            cov = np.cov(recent_sector, recent_mkt)[0, 1]
            var = np.var(recent_mkt)
            betas[sector_name] = cov / var if var > 0 else 1.0

        self._beta_cache = betas
        return betas

    def _beta_neutralize(self, industries: pd.Series, scores: pd.Series) -> pd.Series:
        """Beta 中性化：scores -= beta * market_mean。

        用横截面回归计算每个板块对沪深300的beta，
        然后减去 beta * 大盘近期均值，消除牛熊整体涨跌影响。
        """
        if industries.empty or len(industries) < 2:
            return scores

        # 用动量得分作为代理收益来计算beta
        mkt_ret = self._get_benchmark_returns()
        if mkt_ret is None or mkt_ret.empty or len(mkt_ret) < 60:
            return scores

        recent_mkt = mkt_ret.tail(60)
        mkt_mean = recent_mkt.mean()

        if abs(mkt_mean) < 1e-8:
            # 大盘均值接近0，无需中性化
            return scores

        # 简化实现：使用板块动量收益做beta
        # 从 composite_scores 中，动量维度已经是0-100的分数
        # 将其映射回收益率空间: (score - 50) / 50 * 0.1 → ±10%收益率
        mapped_scores = ((scores - 50) / 50 * 0.1).values

        # 计算每个板块的beta
        # 这里简化：用动量得分的偏离度作为beta代理
        # 动量得分 > 50 的板块 beta > 1（超大盘股特征）
        # 动量得分 < 50 的板块 beta < 1（防御股特征）
        raw_betas = (scores / 50).clip(0.5, 1.5).values

        # Beta 中性化: adjusted = raw - beta * market_mean
        neutralized = scores.values - raw_betas * mkt_mean * 100

        # 截断到合理范围
        return pd.Series(np.clip(neutralized, 0, 100), index=scores.index)
