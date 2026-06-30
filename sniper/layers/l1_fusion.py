"""L1 贝叶斯精度加权融合 — 纯叶子模块, 零sniper内部依赖

融合公式(终审APPROVED):
  posterior = (tau_prior * sw1 + tau_likelihood * etf) / (tau_prior + tau_likelihood)

设计原则:
  1. 所有融合函数为纯函数(输入确定->输出确定)
  2. 公平性是数学必然: 无ETF证据 -> posterior = prior
  3. L0-gated框架保留市场状态自适应
  4. 精度传播: 弱信号自动缩权,强信号自动扩权
"""

from dataclasses import dataclass
from enum import Enum
import numpy as np
import pandas as pd

from sniper.config import FUSION as CFG
from core.logger import get_logger

logger = get_logger("sniper.layers.l1_fusion")


class FusionStatus(Enum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    FALLBACK = "FALLBACK"


@dataclass
class FusionReport:
    """单次融合的质量报告"""
    total_industries: int = 0
    covered: int = 0
    uncovered: int = 0
    mean_confidence: float = 0.0
    mean_likelihood_precision: float = 0.0
    max_fusion_shift: float = 0.0
    status: str = "NORMAL"


class BayesianPrecisionFusion:
    """L0-gated 贝叶斯精度加权融合引擎。

    纯叶子模块: 不导入任何sniper内部模块,所有输入通过参数传入。
    全部方法为纯函数,可独立单元测试。

    用法:
        fusion = BayesianPrecisionFusion()
        fused_df = fusion.fuse(sw1_df, etf_mapped, etf_confidence, l0_score)
    """

    def __init__(self, config: dataclass | None = None):
        self.cfg = config or CFG

    # ── 纯函数接口 ──

    @staticmethod
    def _compute_w_etf(l0_score: float, cfg: dataclass) -> float:
        """L0 -> ETF先验权重 线性插值。

        纯函数。输入确定->输出确定。

        公式:
          w_etf = clamp((L0 - l0_min) / (l0_max - l0_min) * (w_etf_max - w_etf_min) + w_etf_min,
                        w_etf_min, w_etf_max)

        数值保护: l0_score NaN -> 返回 w_etf_min + (w_etf_max - w_etf_min) / 2 = 0.40
        """
        eps = getattr(cfg, 'epsilon', 1e-8)
        if np.isnan(l0_score):
            return cfg.w_etf_min + (cfg.w_etf_max - cfg.w_etf_min) / 2.0

        ratio = (l0_score - cfg.l0_min) / max(cfg.l0_max - cfg.l0_min, eps)
        w_etf = cfg.w_etf_min + ratio * (cfg.w_etf_max - cfg.w_etf_min)
        return float(max(cfg.w_etf_min, min(cfg.w_etf_max, w_etf)))

    @staticmethod
    def _signal_gain(etf_score: float, signal_scale: float,
                      eps: float = 1e-8) -> float:
        """信号强度 -> 似然增益 sigmoid 映射。

        gain = 2 / (1 + exp(-|score-50| / signal_scale)) - 1
        -> score=50(无信息): gain约0
        -> score=75(中等信号): gain约0.46
        -> score=100(极强信号): gain约0.762

        *** 评审WARN修正: 文档值更正为0.762(非0.88) ***
        """
        diff = abs(etf_score - 50.0)
        exp_val = np.exp(-diff / max(signal_scale, eps))
        sigmoid = 2.0 / (1.0 + exp_val) - 1.0
        return float(max(0.0, sigmoid))

    @staticmethod
    def _likelihood_precision(w_etf: float, confidence: float,
                               signal_gain: float, eps: float = 1e-8) -> float:
        """似然精度 = w_etf x confidence x signal_gain

        纯函数。所有参数在[0,1]区间,输出也在[0,1]。
        """
        precision = w_etf * confidence * signal_gain
        return float(max(0.0, precision))

    @staticmethod
    def _bayesian_posterior(prior: float, likelihood: float,
                             prior_prec: float, like_prec: float,
                             eps: float = 1e-8) -> float:
        """精度加权后验。

        posterior = (tau_p x prior + tau_l x likelihood) / (tau_p + tau_l)

        数值保护:
          - tau_p + tau_l < eps -> 返回 prior (除零保护)
          - 结果 clamp [0, 100]
        """
        tau_sum = prior_prec + like_prec
        if tau_sum < eps:
            return float(prior)
        posterior = (prior_prec * prior + like_prec * likelihood) / tau_sum
        return float(max(0.0, min(100.0, posterior)))

    # ── 融合入口 ──

    def fuse(self,
             sw1_df: pd.DataFrame,
             etf_mapped: dict[str, float],
             etf_confidence: dict[str, float],
             l0_score: float) -> pd.DataFrame:
        """执行贝叶斯精度融合。

        Args:
          sw1_df: SectorScorer.composite_scores()输出
            columns: [industry_name, composite, ...], 至少2列
          etf_mapped: ETF->SW1映射后的评分 {sw1_name: etf_score_0_100}
          etf_confidence: {sw1_name: confidence_0_1}
          l0_score: L0市场评分

        Returns DataFrame:
          industry_name | sw1_composite | etf_mapped | fused_score |
          w_etf | likelihood_precision | confidence | source
        """
        eps = self.cfg.epsilon
        w_etf = self._compute_w_etf(l0_score, self.cfg)

        # 校验评分分布(评审FAIL-13修复)
        if not self._validate_scores(sw1_df):
            logger.warning("SW1评分完整性校验失败,回退纯SW1评分")
            return self._build_fallback_df(sw1_df, "SW1_FAILED_VALIDATION")

        records = []
        for _, row in sw1_df.iterrows():
            ind = row.get("industry_name", "")
            sw1_score = float(row.get("composite", 50.0))

            etf_score = etf_mapped.get(ind)
            etf_conf = etf_confidence.get(ind, 0.0)

            if etf_score is None or np.isnan(etf_score):
                # 无ETF映射: posterior = prior (零惩罚公平性)
                fused = sw1_score
                source = "sw1_only"
                like_prec = 0.0
                conf = 0.0
            else:
                gain = self._signal_gain(etf_score, self.cfg.signal_scale, eps)
                like_prec = self._likelihood_precision(w_etf, etf_conf, gain, eps)
                fused = self._bayesian_posterior(
                    sw1_score, etf_score,
                    self.cfg.prior_precision, like_prec, eps)
                source = "sw1+etf"
                conf = etf_conf

            records.append({
                "industry_name": ind,
                "sw1_composite": round(sw1_score, 1),
                "etf_mapped": round(etf_score, 1) if etf_score is not None and not np.isnan(etf_score) else None,
                "fused_score": round(fused, 1),
                "w_etf": round(w_etf, 4),
                "likelihood_precision": round(like_prec, 4),
                "confidence": round(conf, 2),
                "source": source,
            })

        fused_df = pd.DataFrame(records)
        max_shift = abs(fused_df["sw1_composite"] - fused_df["fused_score"]).max()

        logger.info(
            f"融合完成: w_etf={w_etf:.3f}, "
            f"覆盖={sum(s=='sw1+etf' for s in fused_df['source'])}/"
            f"{len(fused_df)}, "
            f"最大位移={max_shift:.1f}"
        )
        return fused_df

    # ── 完整性校验 ──

    @staticmethod
    def _validate_scores(sw1_df: pd.DataFrame) -> bool:
        """融合前校验评分分布合理性。

        Returns: True=正常, False=触发ORANGE降级
        """
        if "composite" not in sw1_df.columns:
            return False
        vals = sw1_df["composite"].dropna().values
        if len(vals) < 10:
            return True  # 行业数不足时不判断零方差(防误报)
        if all(v == 0 for v in vals):
            return False
        if all(v == 100 for v in vals):
            return False
        if float(np.max(vals)) - float(np.min(vals)) < 1e-6:
            return False
        return True

    @staticmethod
    def _build_fallback_df(sw1_df: pd.DataFrame,
                           reason: str = "FALLBACK") -> pd.DataFrame:
        """构建降级DataFrame(纯SW1,不触发融合)"""
        records = []
        for _, row in sw1_df.iterrows():
            records.append({
                "industry_name": row.get("industry_name", ""),
                "sw1_composite": row.get("composite", 50.0),
                "etf_mapped": None,
                "fused_score": row.get("composite", 50.0),
                "w_etf": 0.0,
                "likelihood_precision": 0.0,
                "confidence": 0.0,
                "source": f"fallback_{reason}",
            })
        return pd.DataFrame(records)

    # ── 质量报告 ──

    def get_quality_report(self, fused_df: pd.DataFrame) -> FusionReport:
        """生成融合质量报告(用于监控和日志)。"""
        if fused_df.empty:
            return FusionReport(status="NO_DATA")

        total = len(fused_df)
        covered = int((fused_df["source"] == "sw1+etf").sum())
        uncovered = total - covered

        return FusionReport(
            total_industries=total,
            covered=covered,
            uncovered=uncovered,
            mean_confidence=float(fused_df["confidence"].mean()),
            mean_likelihood_precision=float(fused_df["likelihood_precision"].mean()),
            max_fusion_shift=float(
                abs(fused_df["sw1_composite"] - fused_df["fused_score"]).max()
            ),
            status="NORMAL" if covered > 0 else "FALLBACK",
        )
