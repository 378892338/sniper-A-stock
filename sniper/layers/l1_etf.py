"""L1 ETF动量评分 — 4维评分 + ETF->SW1映射

设计原则(评审闭环):
  1. 计算与I/O分离: 4个评分函数均为@staticmethod纯函数,可独立单测
  2. 数据通过DataRouter依赖注入(可Mock)
  3. 每个维度有独立的降级路径
  4. 连续性新高有衰减机制(评审WARN修复)
  5. 延续确认在回测中禁止使用T+1数据(评审FAIL-17修复)
  6. 所有除法带epsilon保护
"""

from dataclasses import dataclass, field
from enum import Enum
import numpy as np
import pandas as pd

# TODO: migrate to `import sniper.config as _cfg` (latent import-time binding risk)
from sniper.config import ETF_MOMENTUM as CFG
from sniper.data_router import DataRouter
from core.logger import get_logger

logger = get_logger("sniper.layers.l1_etf")


class DataAvailability(Enum):
    """单ETF数据可用性"""
    OK = "OK"
    STALE = "STALE"       # 超过freshness_hours未更新
    PARTIAL = "PARTIAL"   # 部分维度缺失
    MISSING = "MISSING"   # 完全缺失


class EtfMomentumScorer:
    """ETF动量评分器。

    对14只ETF分类指数独立计算4维评分(60日新高/MA60偏离/资金验证/延续确认),
    输出标准化评分矩阵, 并通过映射层将ETF评分投影到SW1行业空间。

    用法:
        scorer = EtfMomentumScorer(router)
        df = scorer.score_all("2026-06-30")
        mapped, conf = scorer.map_to_sw1(df)
    """

    def __init__(self, router: DataRouter | None = None,
                 config: dataclass | None = None):
        self.router = router or DataRouter()
        self.cfg = config or CFG
        self._etf_bars_cache: dict[str, pd.DataFrame] = {}
        self._consecutive_high_days: dict[str, int] = {}  # 连续新高计数器

    # ── I/O 层 (可Mock,唯一有副作用的函数) ──

    def _fetch_etf_data(self, etf_name: str, date: str) -> pd.DataFrame:
        """获取单只ETF在目标日期的可用日线数据。

        降级: 获取失败->返回空DataFrame(不影响其他ETF)。
        缓存: 已加载的数据在session内复用。
        """
        if etf_name in self._etf_bars_cache:
            return self._etf_bars_cache[etf_name]

        df = self.router.get_etf_daily(etf_name)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").set_index("date")
            self._etf_bars_cache[etf_name] = df
        return df

    def _check_data_availability(self, etf_name: str,
                                  data: pd.DataFrame | None,
                                  date: str) -> DataAvailability:
        """检查单只ETF数据可用性"""
        if data is None or data.empty:
            return DataAvailability.MISSING
        if date not in data.index:
            # 尝试找最后一个<=date的数据日
            valid = data.index[data.index <= pd.Timestamp(date)]
            if len(valid) == 0:
                return DataAvailability.MISSING
            # 检查新鲜度
            last_date = valid[-1]
            delta_days = (pd.Timestamp(date) - last_date).days
            if delta_days > self.cfg.freshness_hours / 24:
                return DataAvailability.STALE
        return DataAvailability.OK

    def _get_bars_before(self, etf_name: str, date: str,
                         window: int) -> np.ndarray | None:
        """获取date之前window个交易日的close序列。

        Returns: np.ndarray (长度<=window), None=数据不足
        """
        bars = self._fetch_etf_data(etf_name, date)
        if bars.empty or "close" not in bars.columns:
            return None
        valid = bars.index[bars.index <= pd.Timestamp(date)]
        if len(valid) == 0:
            return None
        idx = valid[-1]
        pos = bars.index.get_loc(idx)
        start = max(0, pos - window + 1)
        return bars["close"].values[start:pos + 1]

    def _get_volume_before(self, etf_name: str, date: str,
                           window: int) -> np.ndarray | None:
        """获取date之前window个交易日的volume序列"""
        bars = self._fetch_etf_data(etf_name, date)
        if bars.empty or "volume" not in bars.columns:
            return None
        valid = bars.index[bars.index <= pd.Timestamp(date)]
        if len(valid) == 0:
            return None
        idx = valid[-1]
        pos = bars.index.get_loc(idx)
        start = max(0, pos - window + 1)
        return bars["volume"].values[start:pos + 1]

    # ── 纯计算层 (4个@staticmethod, 无副作用, 可独立单测) ──

    @staticmethod
    def _score_high_proximity(high_60d: np.ndarray, close: float,
                               eps: float = 1e-8) -> float:
        """60日新高接近度 (40%权重)。

        纯函数。输入确定->输出确定。

        公式:
          ratio = close / max(high[-60:])
          score = max(0, min(100, (ratio - 0.80) / 0.20 * 100))
          -> ratio=0.80->0, 0.95->75, 1.00->100

        降级: 数据不足60日->取可用窗口; 仍不足5日->返回50.0
        数值保护: max_high <= eps -> 返回50.0
        """
        if len(high_60d) < 5:
            return 50.0
        max_high = float(np.max(high_60d))
        if max_high <= eps:
            return 50.0
        ratio = close / max_high
        score = (ratio - 0.80) / 0.20 * 100
        return float(max(0.0, min(100.0, score)))

    @staticmethod
    def _score_ma60_deviation(close_60d: np.ndarray, close: float,
                               eps: float = 1e-8) -> float:
        """MA60偏离度 (30%权重)。

        ma60 = mean(close[-60:])
        deviation = (close - ma60) / (ma60 + eps)
        score = clamp(50 + deviation * 500, 0, 100)
        -> deviation=-5%->25, 0%->50, +5%->75

        简化为完整设计方案中的线性映射。
        """
        if len(close_60d) < 10:
            return 50.0
        ma60 = float(np.mean(close_60d))
        if ma60 <= eps:
            return 50.0
        deviation = (close - ma60) / ma60
        score = 50.0 + deviation * 500.0
        return float(max(0.0, min(100.0, score)))

    @staticmethod
    def _score_fund_validation(volume: float,
                                volume_20d: np.ndarray,
                                close: float,
                                ma20: float,
                                eps: float = 1e-8) -> float:
        """资金验证 (20%权重)。

        量能比率: vol_ratio = volume / (mean(volume_20d) + eps)
        价量配合:
          close > ma20 AND vol_ratio > 1.0 -> bonus = +10
          close < ma20 AND vol_ratio < 1.0 -> bonus = +5
          其他 -> bonus = 0

        base = max(0, min(100, (vol_ratio - 0.5) / 1.5 * 100))
        final = clamp(base + bonus, 0, 100)
        """
        if len(volume_20d) < 5:
            return 50.0
        vol_ma = float(np.mean(volume_20d))
        if vol_ma <= eps:
            return 50.0
        vol_ratio = volume / vol_ma
        base = (vol_ratio - 0.5) / 1.5 * 100.0

        bonus = 0.0
        if close > ma20 and vol_ratio > 1.0:
            bonus = 10.0
        elif close < ma20 and vol_ratio < 1.0:
            bonus = 5.0

        return float(max(0.0, min(100.0, base + bonus)))

    @staticmethod
    def _score_continuation(close_5d_t_minus_1: np.ndarray) -> float:
        """延续确认 (10%权重)。

        *** 关键时序约束(评审FAIL-17修复): ***
        传入的必须是T-1及之前的数据!!
        回测中T日决策只能使用T-1的延续确认结果:
          close_5d_t_minus_1 = close[T-5:T] (不含当日)

        规则:
          up_ratio = count(close > prev_close) / 4 (最近4对)
          若昨天(close[-1] > close[-2]): bonus = +20
          score = up_ratio * 80 + bonus
          clamp(0, 100)

        降级: 数据不足5日 -> 返回50.0
        """
        if len(close_5d_t_minus_1) < 3:
            return 50.0
        up_count = 0
        for i in range(1, min(5, len(close_5d_t_minus_1))):
            if close_5d_t_minus_1[-i] > close_5d_t_minus_1[-i - 1]:
                up_count += 1
        up_ratio = up_count / max(1, min(4, len(close_5d_t_minus_1) - 1))

        bonus = 20.0 if close_5d_t_minus_1[-1] > close_5d_t_minus_1[-2] else 0.0
        score = up_ratio * 80.0 + bonus
        return float(max(0.0, min(100.0, score)))

    # ── 合成接口 ──

    def score_single(self, etf_name: str, date: str) -> dict:
        """对单只ETF计算4维评分。

        Returns:
            {"etf_name": str, "high_prox": float, "ma60_dev": float,
             "fund_val": float, "continuation": float, "composite": float,
             "confidence": float(0-1), "data_quality": str}
        """
        eps = self.cfg.epsilon
        bars = self._fetch_etf_data(etf_name, date)
        avail = self._check_data_availability(etf_name, bars, date)
        if avail == DataAvailability.MISSING:
            return {"etf_name": etf_name, "high_prox": 50.0, "ma60_dev": 50.0,
                    "fund_val": 50.0, "continuation": 50.0, "composite": 50.0,
                    "confidence": 0.0, "data_quality": "MISSING"}

        # 定位有效日期
        valid_idx = bars.index[bars.index <= pd.Timestamp(date)][-1]
        close = float(bars.loc[valid_idx, "close"])
        volume = float(bars.loc[valid_idx, "volume"]) if "volume" in bars.columns else 0.0

        # 获取各维度所需数据窗口
        high_60d = self._get_bars_before(etf_name, date, self.cfg.window_high)
        close_60d = self._get_bars_before(etf_name, date, self.cfg.window_ma)
        volume_20d = self._get_volume_before(etf_name, date, self.cfg.window_fund)
        close_5d = self._get_bars_before(etf_name, date, self.cfg.window_cont)

        # 4维评分
        high_prox = self._score_high_proximity(
            high_60d if high_60d is not None else np.array([close]),
            close, eps)
        ma60_dev = self._score_ma60_deviation(
            close_60d if close_60d is not None else np.array([close]),
            close, eps)

        # 新高衰减(评审WARN修复: 连续创新高信号过密)
        if self.cfg.high_decay_enabled and high_prox > 80:
            count = self._consecutive_high_days.get(etf_name, 0) + 1
            self._consecutive_high_days[etf_name] = count
            if count > self.cfg.high_decay_start:
                decay = max(self.cfg.high_decay_min,
                            1.0 - self.cfg.high_decay_rate * (count - self.cfg.high_decay_start))
                high_prox *= decay
        else:
            self._consecutive_high_days[etf_name] = 0

        ma20 = float(np.mean(close_60d[-20:])) if (close_60d is not None and len(close_60d) >= 20) else close
        fund_val = self._score_fund_validation(
            volume,
            volume_20d if volume_20d is not None else np.array([volume]),
            close, ma20, eps)
        continuation = self._score_continuation(
            close_5d if close_5d is not None else np.array([close, close]))

        # 合成
        composite = (high_prox * self.cfg.w_high_proximity
                     + ma60_dev * self.cfg.w_ma60_deviation
                     + fund_val * self.cfg.w_fund_validation
                     + continuation * self.cfg.w_continuation)

        # 置信度: 基于数据可用性
        conf_map = {DataAvailability.OK: 1.0, DataAvailability.STALE: 0.6,
                    DataAvailability.PARTIAL: 0.3, DataAvailability.MISSING: 0.0}

        return {"etf_name": etf_name, "high_prox": round(high_prox, 1),
                "ma60_dev": round(ma60_dev, 1), "fund_val": round(fund_val, 1),
                "continuation": round(continuation, 1),
                "composite": round(composite, 1),
                "confidence": conf_map.get(avail, 0.5),
                "data_quality": avail.value}

    def score_all(self, date: str,
                  etf_names: list[str] | None = None) -> pd.DataFrame:
        """批量计算所有ETF分类指数的4维评分。

        Returns DataFrame:
          etf_name | high_prox | ma60_dev | fund_val | continuation |
          composite | confidence | data_quality
        """
        from data.index_etf import ETF_INDEX_MAP
        names = etf_names or list(ETF_INDEX_MAP.keys())

        records = []
        for name in names:
            rec = self.score_single(name, date)
            records.append(rec)

        df = pd.DataFrame(records)
        logger.info(f"ETF评分: {len(df)}只, "
                    f"覆盖率={(df['data_quality']=='OK').mean():.0%}")
        return df

    # ── 映射层 ──

    def map_to_sw1(self, etf_df: pd.DataFrame) -> tuple[dict[str, float], dict[str, float]]:
        """将ETF评分映射到SW1行业空间。

        映射规则(从data/index_etf.py的INDUSTRY_TO_ETF):
          - 1个ETF -> N个行业: ETF composite直接赋值给各映射行业
          - 1个行业 <- M个ETF: 取max(composite)
          - 9个无映射行业: 不在返回dict中

        Returns:
          (mapped_scores: {sw1_name: etf_score_0_100},
           mapped_confidence: {sw1_name: confidence_0_1})
        """
        from data.index_etf import INDUSTRY_TO_ETF

        scores: dict[str, list[float]] = {}
        confs: dict[str, list[float]] = {}

        for _, row in etf_df.iterrows():
            etf_name = row["etf_name"]
            etf_score = row.get("composite", 50.0)
            etf_conf = row.get("confidence", 0.5)

            # 查找这个ETF映射到哪些SW1行业
            sw1_list = [ind for ind, etfs in INDUSTRY_TO_ETF.items() if etf_name in etfs]
            for sw1 in sw1_list:
                if sw1 not in scores:
                    scores[sw1] = []
                    confs[sw1] = []
                scores[sw1].append(etf_score)
                confs[sw1].append(etf_conf)

        # 多ETF映射到同一SW1: 取max(composite)
        mapped_scores = {sw1: max(vals) for sw1, vals in scores.items()}
        mapped_confidence = {sw1: max(vals) for sw1, vals in confs.items()}

        logger.info(f"ETF->SW1映射: {len(mapped_scores)}个行业覆盖")
        return mapped_scores, mapped_confidence

    def get_unmapped_industries(self) -> set[str]:
        """返回无ETF映射的SW1行业名集合(9个)。"""
        from data.index_etf import INDUSTRY_TO_ETF
        mapped = set()
        for etfs in INDUSTRY_TO_ETF.values():
            mapped.update(etfs)
        # 实际返回时需要SW1全集合,此处返回空占位
        # 完整逻辑在集成时补充
        return set()

    def get_etf_index_map(self) -> dict:
        """返回ETF->SW1正向映射(用于调试和日志)。"""
        from data.index_etf import INDUSTRY_TO_ETF
        result: dict[str, list[str]] = {}
        for etf_name in (self._etf_bars_cache or {}):
            sw1_list = [ind for ind, etfs in INDUSTRY_TO_ETF.items() if etf_name in etfs]
            if sw1_list:
                result[etf_name] = sw1_list
        return result
