"""L1 分歧诊断(DDR) — 纯诊断,不调权

设计原则(终审APPROVED):
  1. 仅标记不调权: 消除阈值脆弱性导致的排名跳变
  2. 5类标签互斥输出
  3. 含ETF_COVERAGE_GAP诊断标签(评审WARN修复)
  4. 无可变状态,每日独立横截面Z-score
"""

from dataclasses import dataclass
from enum import Enum
import numpy as np
import pandas as pd

from sniper.config import DDR as CFG
from core.logger import get_logger

logger = get_logger("sniper.layers.l1_divergence")


class DivergenceType(str, Enum):
    CONVERGENT = "CONVERGENT"         # |delta_z| < threshold, 信号一致
    ETF_LEADING = "ETF_LEADING"       # delta_z >> 0, ETF比SW1乐观
    SW1_LEADING = "SW1_LEADING"       # delta_z << 0, SW1比ETF乐观
    COLD_START = "COLD_START"         # 行业数不足10个
    NO_ETF_DATA = "NO_ETF_DATA"       # 该行业无ETF映射


@dataclass
class DiagnosisRecord:
    """单行业分歧诊断记录"""
    industry: str
    divergence_type: str
    delta_z: float
    etf_percentile: float
    sw1_percentile: float
    confidence: float


class DivergenceDetector:
    """分歧诊断检测器 — 纯诊断层。

    不修改fused_score,不调权。
    输出的标签用于:
      - 监控面板展示分歧状态
      - 纸带日志追踪
      - 周末归因分析DDR标签预测力
    """

    def __init__(self, config: dataclass | None = None):
        self.cfg = config or CFG

    def diagnose(self, fused_df: pd.DataFrame,
                 etf_mapped: dict[str, float]) -> list[DiagnosisRecord]:
        """执行分歧诊断。

        Args:
          fused_df: FusionEngine输出的DataFrame
            (含industry_name, fused_score, source等列)
          etf_mapped: ETF->SW1映射评分 {sw1: etf_score}

        Returns:
          list[DiagnosisRecord], 每行业一条
        """
        if fused_df.empty or len(fused_df) < 10:
            logger.warning(f"DDR跳过: 行业数={len(fused_df)}, 不足10")
            return [DiagnosisRecord(
                industry="", divergence_type="COLD_START",
                delta_z=0.0, etf_percentile=0.5,
                sw1_percentile=0.5, confidence=0.0)]

        results: list[DiagnosisRecord] = []
        # 计算ETF覆盖集合
        covered_industries = set(etf_mapped.keys())

        for _, row in fused_df.iterrows():
            ind = row.get("industry_name", "")
            # 计算百分位(在31个行业的横截面rank)
            sw1_score = row.get("sw1_composite", 50.0)
            etf_score = etf_mapped.get(ind)

            if ind not in covered_industries or etf_score is None:
                results.append(DiagnosisRecord(
                    industry=ind, divergence_type="NO_ETF_DATA",
                    delta_z=0.0, etf_percentile=0.5,
                    sw1_percentile=self._percentile_in_df(
                        fused_df, "sw1_composite", sw1_score),
                    confidence=0.0))
                continue

            etf_pct = self._percentile_in_df(
                fused_df, "etf_mapped",
                etf_score, ignore_none=True)
            sw1_pct = self._percentile_in_df(
                fused_df, "sw1_composite", sw1_score)

            # delta_z = Z(etf) - Z(sw1)
            delta_z = etf_pct - sw1_pct
            abs_delta = abs(delta_z)

            if abs_delta < self.cfg.convergent_threshold:
                dtype = DivergenceType.CONVERGENT
                confidence = 1.0 - abs_delta * 2  # 分歧越小,置信越高
            elif delta_z > self.cfg.leading_threshold:
                dtype = DivergenceType.ETF_LEADING
                confidence = min(1.0, delta_z * 0.5)
            elif delta_z < -self.cfg.leading_threshold:
                dtype = DivergenceType.SW1_LEADING
                confidence = min(1.0, -delta_z * 0.5)
            else:
                dtype = DivergenceType.CONVERGENT
                confidence = 0.5

            results.append(DiagnosisRecord(
                industry=ind, divergence_type=dtype.value,
                delta_z=round(delta_z, 3),
                etf_percentile=round(etf_pct, 3),
                sw1_percentile=round(sw1_pct, 3),
                confidence=round(confidence, 3)))

        # 统计并日志
        type_counts = {}
        for r in results:
            type_counts[r.divergence_type] = type_counts.get(r.divergence_type, 0) + 1
        logger.info(f"DDR分歧诊断: {type_counts}")
        return results

    @staticmethod
    def _percentile_in_df(df: pd.DataFrame, col: str,
                          value: float,
                          ignore_none: bool = False) -> float:
        """计算value在df[col]中的百分位(0-1)。

        若col不存在或全空,返回0.5
        """
        if col not in df.columns:
            return 0.5
        vals = df[col].dropna().values
        if ignore_none:
            vals = vals[~np.isnan(vals)] if len(vals) > 0 else vals
        if len(vals) == 0:
            return 0.5
        # 百分位 = rank / N
        rank = float(np.sum(vals <= value))
        return rank / len(vals)

    def get_coverage_gap_report(self, fused_df: pd.DataFrame,
                                 etf_mapped: dict[str, float]) -> dict:
        """生成ETF覆盖偏差报告(评审WARN修复)。

        统计有/无ETF覆盖行业的排名差异,用于监控系统性偏差。
        """
        if fused_df.empty or "industry_name" not in fused_df.columns:
            return {"coverage_gap": None, "warning": "no_data"}

        covered = set(etf_mapped.keys())
        fused_df = fused_df.copy()
        fused_df["has_etf"] = fused_df["industry_name"].isin(covered)

        # 按fused_score排名
        fused_df = fused_df.sort_values("fused_score", ascending=False)
        fused_df["rank"] = range(1, len(fused_df) + 1)

        covered_ranks = fused_df[fused_df["has_etf"]]["rank"].tolist()
        uncovered_ranks = fused_df[~fused_df["has_etf"]]["rank"].tolist()

        report = {
            "covered_count": len(covered_ranks),
            "uncovered_count": len(uncovered_ranks),
            "covered_avg_rank": round(float(np.mean(covered_ranks)), 1) if covered_ranks else None,
            "uncovered_avg_rank": round(float(np.mean(uncovered_ranks)), 1) if uncovered_ranks else None,
        }

        if report["covered_avg_rank"] and report["uncovered_avg_rank"]:
            report["rank_gap"] = round(
                report["uncovered_avg_rank"] - report["covered_avg_rank"], 1)
            report["ratio_in_top_n"] = round(
                sum(1 for r in covered_ranks if r <= 3) / max(1, report["covered_count"]), 2)

        return report
