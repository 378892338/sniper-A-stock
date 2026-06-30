"""L0 市场状态评分 — 多周期趋势 + 量能 + 宽度 + 北向"""

import pandas as pd
import numpy as np

import sniper.config as CFG
from sniper.data_router import DataRouter
from core.logger import get_logger

logger = get_logger("sniper.layers.l0_market")


class MarketScorer:
    """市场状态评分器。每个交易日返回 0-100 的综合评分。

    三维评分（参考老版 fractal backtest 已验证逻辑）:
      趋势(40%): MA250位置 + MA20/MA60排列 + 短线偏离
      量能(30%): 成交额 vs MA20
      宽度(20%): 上涨家数占比
      北向(10%): 北向资金净流入
    """

    def __init__(self, router: DataRouter | None = None):
        self.router = router or DataRouter()
        self._trend_cache: dict[str, float] = {}

    def score_trend(self, date: str) -> float:
        """趋势维度 0-100。

        使用多周期趋势，全部改用线性插值消除硬阈值跳变：
          1. MA250 位置：close/MA250 比率线性映射，无跳变
          2. MA20 vs MA60 排列：比率线性映射
          3. 短线偏离 MA20：线性映射（已有）
        """
        bm = self.router.get_index_daily("csi300")
        if bm.empty or "close" not in bm.columns:
            return 50.0

        bm = bm.sort_values("date")
        bm["close"] = bm["close"].astype(float)
        bm_row = bm[bm["date"] <= date]
        if bm_row.empty:
            return 50.0

        close = bm_row["close"].values
        score = 50.0

        # 1. MA250 位置 — 线性映射：ratio 0.90→-20, 1.00→0, 1.05→+25
        if len(close) >= 250:
            ma250 = np.mean(close[-250:])
            ratio = close[-1] / ma250
            score += max(-20, min(25, (ratio - 1.0) * 500))
        else:
            score += 5

        # 2. MA20 vs MA60 排列 — 线性映射：ratio 0.97→-20, 1.00→0, 1.03→+25
        if len(close) >= 60:
            ma20 = np.mean(close[-20:])
            ma60 = np.mean(close[-60:])
            ratio = ma20 / ma60
            score += max(-20, min(25, (ratio - 1.0) * 833))
        else:
            score += 5

        # 3. 短线偏离 MA20（已为线性，保持）
        if len(close) >= 20:
            ma20_short = np.mean(close[-20:])
            deviation = (close[-1] - ma20_short) / ma20_short if ma20_short > 0 else 0
            short_score = (deviation / 0.03) * 15
            score += max(-15, min(15, short_score))

        return max(0.0, min(100.0, score))

    def score_volume(self, date: str) -> float:
        """量能维度 0-100：成交量相对 MA 的活跃度。"""
        df = self.router.get_market_volume(window=CFG.MARKET.volume_window)
        if df.empty:
            return 50.0
        row = df[df["date"] <= date]
        if row.empty:
            return 50.0
        latest = row.iloc[-1]
        ratio = latest.get("volume_ratio")
        if not ratio or pd.isna(ratio):
            return 50.0
        # 0.5 → 0, 1.0 → 50, 2.0 → 100
        score = (ratio - 0.5) / 1.5 * 100
        return max(0.0, min(100.0, score))

    def score_breadth(self, date: str) -> float:
        """宽度维度 0-100：上涨家数占比。"""
        breadth = self.router.get_market_breadth(date)
        return breadth["ratio"] * 100

    def score_northbound(self, date: str) -> float:
        """北向资金维度 0-100。

        真实数据可用时使用真实值，否则从趋势维度纯趋势合成。
        东财自 2024-08 后停止发布北向实时数据，DB 中均为 0。
        纯趋势合成理由：北向资金流向与大盘趋势高度相关。
        """
        dates = self.router.get_trading_days_before(date, CFG.MARKET.northbound_window)
        if not dates:
            return self._synthetic_northbound(date)
        df = self.router.get_northbound_net(dates[0], date)
        if df.empty:
            return self._synthetic_northbound(date)
        avg = df["total_net"].tail(CFG.MARKET.northbound_window).mean()
        # DB 中无效数据被存为精确 0.0（真实北向最低百万级）
        if pd.isna(avg) or abs(avg) < 1:
            return self._synthetic_northbound(date)
        score = 50 + (avg / 5e9) * 50
        return max(0.0, min(100.0, score))

    def _synthetic_northbound(self, date: str) -> float:
        """纯趋势合成北向评分。北向不可用时，用趋势直接替代。"""
        return self.score_trend(date)

    def composite_score(self, date: str) -> float:
        """4 维合成评分 0-100。"""
        return self.score_all(date)["composite"]

    def score_all(self, date: str) -> dict:
        """返回合成分 + 各子维度评分 + 数据质量标记。"""
        scores = {}
        scores["trend"] = self.score_trend(date)
        scores["volume"] = self.score_volume(date)
        scores["breadth"] = self.score_breadth(date)
        scores["northbound"] = self.score_northbound(date)

        composite = (
            scores["trend"] * CFG.MARKET.trend_weight
            + scores["volume"] * CFG.MARKET.volume_weight
            + scores["breadth"] * CFG.MARKET.breadth_weight
            + scores["northbound"] * CFG.MARKET.northbound_weight
        )
        scores["composite"] = composite

        # 数据质量标记：检测 breadth 计算使用的股票覆盖率
        # 当 daily_bars 覆盖率不足 50% 时，L0 合成不可靠
        try:
            from data.local.warehouse import LocalDataWarehouse
            conn = LocalDataWarehouse()._connect()
            import pandas as _pd
            total = _pd.read_sql("SELECT COUNT(*) as c FROM stock_list WHERE status='active'", conn)
            covered = _pd.read_sql(
                "SELECT COUNT(DISTINCT symbol) as c FROM daily_bars WHERE date=?",
                conn, params=(date,),
            )
            conn.close()
            t = int(total["c"].iloc[0]) if not total.empty else 0
            c = int(covered["c"].iloc[0]) if not covered.empty else 0
            scores["_total_stocks"] = t
            scores["_covered_stocks"] = c
            if t > 0 and c / t < 0.5:
                scores["_data_quality"] = "UNRELIABLE"
            else:
                scores["_data_quality"] = "OK"
        except Exception:
            scores["_data_quality"] = "UNKNOWN"

        logger.info(
            f"L0 {date}: 趋势={scores['trend']:.1f} 量能={scores['volume']:.1f} "
            f"宽度={scores['breadth']:.1f} 北向={scores['northbound']:.1f} "
            f"合成={composite:.1f} "
            f"数据质量={scores['_data_quality']}"
        )
        return scores

    def market_regime(self, date: str) -> str:
        """判断市场状态: bullish / neutral / bearish。"""
        score = self.composite_score(date)
        if score >= CFG.MARKET.bullish_threshold:
            return "bullish"
        elif score <= CFG.MARKET.bearish_threshold:
            return "bearish"
        return "neutral"
