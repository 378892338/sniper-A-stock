"""绩效评估器 — 每日/周/月频评估 + 参数改进建议"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

from core.logger import get_logger

logger = get_logger("sniper.engine.evaluator")

EVAL_DIR = Path(__file__).resolve().parents[2] / "outputs" / "evaluations"


class PerformanceEvaluator:
    """绩效评估器。

    功能:
      - 每日更新：跟踪当日收益、回撤
      - 周报：按周聚合统计
      - 月频校准：参数敏感性分析 + 改进建议
    """

    def __init__(self, initial_capital: float = 1_000_000):
        self.initial_capital = initial_capital
        EVAL_DIR.mkdir(parents=True, exist_ok=True)

    # ── 日频更新 ──

    def daily_update(self, date: str, total_value: float, nav: float,
                     drawdown: float, daily_pnl: float, positions: int) -> dict:
        """每日更新：写入日绩效记录。"""
        record = {
            "date": date,
            "total_value": round(total_value, 2),
            "nav": round(nav, 4),
            "drawdown": round(drawdown, 4),
            "daily_pnl": round(daily_pnl, 4),
            "positions": positions,
        }

        self._append_daily(record)
        return record

    def _append_daily(self, record: dict):
        """追加日绩效记录到 parquet。"""
        fpath = EVAL_DIR / "daily_metrics.parquet"
        df = pd.DataFrame([record])
        if fpath.exists():
            try:
                old = pd.read_parquet(fpath)
                # 去重（按 date）
                old = old[old["date"] != record["date"]]
                df = pd.concat([old, df], ignore_index=True)
            except Exception:
                pass
        df.to_parquet(fpath, index=False)

    def load_daily(self, start: str = "", end: str = "") -> pd.DataFrame:
        """加载日绩效数据。"""
        fpath = EVAL_DIR / "daily_metrics.parquet"
        if not fpath.exists():
            return pd.DataFrame()
        df = pd.read_parquet(fpath)
        if start:
            df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        return df.sort_values("date").reset_index(drop=True)

    # ── 周频报告 ──

    def weekly_report(self, as_of: str) -> dict:
        """生成周绩效报告。"""
        daily = self.load_daily(end=as_of)
        if daily.empty:
            return {}

        # 取最近 5 个交易日（约 1 周）
        weekly = daily.tail(5)
        if weekly.empty:
            return {}

        returns = weekly["daily_pnl"].values
        navs = weekly["nav"].values

        total_return = navs[-1] / navs[0] - 1 if navs[0] > 0 else 0
        peak = np.maximum.accumulate(navs)
        dd = (navs - peak) / peak

        report = {
            "date": as_of,
            "period": "weekly",
            "total_return": round(total_return, 4),
            "avg_daily_pnl": round(np.mean(returns), 4),
            "std_daily_pnl": round(np.std(returns, ddof=1), 4),
            "max_drawdown": round(abs(np.min(dd)), 4),
            "avg_positions": round(weekly["positions"].mean(), 1),
            "end_nav": round(navs[-1], 4),
        }

        self._save_report(report)
        return report

    # ── 月频校准 ──

    def monthly_calibration(self, as_of: str,
                            trade_log_df: pd.DataFrame | None = None) -> dict:
        """月频校准：参数敏感性分析 + 改进建议。

        Args:
            as_of: 评估日期
            trade_log_df: 该月的交易日志 DataFrame

        Returns:
            suggestions: 参数改进建议
            {
                "stop_loss": {"current": -0.08, "optimal": -0.10, "impact": 0.05, "confidence": 0.7},
                ...
            }
        """
        daily = self.load_daily(end=as_of)
        if daily.empty:
            return {"date": as_of, "suggestions": {}, "metrics": {}}

        # 最近 20 个交易日（约 1 个月）
        monthly = daily.tail(20)
        if monthly.empty:
            return {"date": as_of, "suggestions": {}, "metrics": {}}

        navs = monthly["nav"].values
        returns = monthly["daily_pnl"].values

        # 月绩效指标
        total_ret = navs[-1] / navs[0] - 1 if navs[0] > 0 else 0
        n_years = 20 / 252
        ann_ret = (1 + total_ret) ** (1 / n_years) - 1 if n_years > 0 else 0
        vol = np.std(returns, ddof=1) * np.sqrt(252)
        sharpe = (ann_ret - 0.02) / vol if vol > 0 else 0
        peak = np.maximum.accumulate(navs)
        dd = (navs - peak) / peak
        max_dd = abs(np.min(dd))

        metrics = {
            "month_return": round(total_ret, 4),
            "annual_return": round(ann_ret, 4),
            "sharpe": round(sharpe, 3),
            "max_drawdown": round(max_dd, 4),
            "volatility": round(vol, 4),
        }

        # 参数敏感性分析（基于交易日志中的参数快照）
        suggestions = self._analyze_param_sensitivity(trade_log_df)

        report = {
            "date": as_of,
            "period": "monthly",
            "metrics": metrics,
            "suggestions": suggestions,
        }

        self._save_report(report)
        return report

    def _analyze_param_sensitivity(self, trade_log_df: pd.DataFrame | None) -> dict:
        """分析交易日志中的参数敏感度。

        对于每个参数，比较"盈利交易"和"亏损交易"中的参数分布差异。
        差异大 = 该参数对绩效影响大。
        """
        if trade_log_df is None or trade_log_df.empty:
            return {}

        # 提取参数快照
        params_records = []
        for _, row in trade_log_df.iterrows():
            ps = row.get("params_snapshot", "")
            if not ps:
                continue
            try:
                params = json.loads(ps) if isinstance(ps, str) else ps
            except (json.JSONDecodeError, TypeError):
                continue

            pnl_pct = row.get("pnl_pct", 0) or 0
            is_win = pnl_pct > 0
            params_records.append({"params": params, "pnl_pct": pnl_pct, "is_win": is_win})

        if len(params_records) < 5:
            return {"note": "样本不足 (<5笔)，跳过参数敏感性分析"}

        # 按参数分组统计
        from collections import defaultdict
        param_values: dict[str, dict] = defaultdict(lambda: {"wins": [], "losses": []})
        for rec in params_records:
            for key, val in rec["params"].items():
                if key in param_values:
                    if rec["is_win"]:
                        param_values[key]["wins"].append(val)
                    else:
                        param_values[key]["losses"].append(val)

        suggestions = {}
        for key, vals in param_values.items():
            wins = vals["wins"]
            losses = vals["losses"]
            if not wins or not losses:
                continue

            # 比较胜方和败方的参数均值差异
            win_mean = np.mean(wins)
            loss_mean = np.mean(losses)
            diff = win_mean - loss_mean

            # 置信度 = 样本量 / (样本量 + 10)
            total_n = len(wins) + len(losses)
            confidence = min(1.0, total_n / (total_n + 10))

            # impact = 标准化差异
            all_vals = wins + losses
            std = np.std(all_vals) if len(all_vals) > 1 else 1.0
            impact = abs(diff) / std if std > 0 else 0

            suggestions[key] = {
                "current": None,  # 由调用方填充
                "optimal": win_mean,
                "impact": round(impact, 3),
                "confidence": round(confidence, 2),
                "sensitivity": round(diff / max(1e-8, abs(win_mean)), 3),
            }

        return suggestions

    def _save_report(self, report: dict):
        """保存评估报告到 parquet。"""
        fpath = EVAL_DIR / "reports.parquet"
        df = pd.DataFrame([report])
        if fpath.exists():
            try:
                old = pd.read_parquet(fpath)
                df = pd.concat([old, df], ignore_index=True)
            except Exception:
                pass
        df.to_parquet(fpath, index=False)

    def load_reports(self, period: str = "") -> pd.DataFrame:
        """加载历史评估报告。"""
        fpath = EVAL_DIR / "reports.parquet"
        if not fpath.exists():
            return pd.DataFrame()
        df = pd.read_parquet(fpath)
        if period:
            df = df[df["period"] == period]
        return df.sort_values("date").reset_index(drop=True)

    def calc_sharpe(self, daily_values: list[dict]) -> float:
        """从 daily_values 计算夏普比率。"""
        if not daily_values:
            return 0.0

        values = [d.get("total_value", 0) for d in daily_values]
        if len(values) < 2:
            return 0.0

        returns = np.diff(values) / np.array(values[:-1])
        n_years = len(returns) / 252
        ann_ret = (1 + np.prod(1 + returns)) ** (1 / n_years) - 1 if n_years > 0 else 0
        vol = np.std(returns, ddof=1) * np.sqrt(252)
        return (ann_ret - 0.02) / vol if vol > 0 else 0.0
