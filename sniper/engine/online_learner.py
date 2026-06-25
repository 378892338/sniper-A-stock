"""在线学习器 — 每笔交易增量更新参数敏感性"""

import json
from pathlib import Path
from collections import defaultdict

import numpy as np

from core.logger import get_logger

logger = get_logger("sniper.engine.online_learner")

LEARNER_DIR = Path(__file__).resolve().parents[2] / "outputs" / "online_learn"


class OnlineParamLearner:
    """在线学习器。

    原理:
      每笔交易结束后，根据该交易的 PnL 和当时的参数快照，
      用增量线性回归估计参数敏感性。

      PnL = β0 + Σ βi × param_i + ε

      βi > 0 → 增大参数 i 有利可图
      βi < 0 → 减小参数 i 有利可图

    用法:
      learner = OnlineParamLearner()
      learner.observe(trade_params, pnl_pct)
      suggestion = learner.suggest()
    """

    def __init__(self, max_history: int = 200,
                 db_path: str | Path | None = None):
        """
        Args:
            max_history: 保留的最大历史观测数
            db_path: 持久化路径
        """
        self.max_history = max_history
        self.db_path = Path(db_path or (LEARNER_DIR / "learner_params.parquet"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 观测历史: [(params_dict, pnl_pct), ...]
        self.history: list[tuple[dict[str, float], float]] = []

        # 参数键列表（自动累积）
        self._param_keys: list[str] = []

        # 系数缓存
        self._coefficients: dict[str, float] = {}

        self._load_history()

    def observe(self, trade_params: dict, pnl_pct: float):
        """记录一笔交易观测。

        每笔交易后调用一次，传入该笔交易的参数快照和收益率。
        """
        # 提取数值参数
        params = {k: v for k, v in trade_params.items()
                  if isinstance(v, (int, float))}
        if not params:
            return

        self.history.append((params, pnl_pct))

        # 自动注册参数键
        for k in params:
            if k not in self._param_keys:
                self._param_keys.append(k)

        # 限制历史长度
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

        # 增量更新系数
        self._update_coefficients()

        # 持久化
        self._save_history()

    def _update_coefficients(self):
        """线性回归更新参数敏感性系数。

        用最近 N 笔交易，对每个参数单独做一元线性回归。
        pnl = β0 + βi × param_i
        """
        if len(self.history) < 5:
            return

        # 取最近可用数据
        recent = self.history[-min(len(self.history), 100):]
        y = np.array([pnl for _, pnl in recent])

        for key in self._param_keys:
            X = np.array([p.get(key, 0) for p, _ in recent])

            # 过滤方差为0的列
            if np.std(X) < 1e-10:
                continue

            # 最小二乘: β = cov(X, y) / var(X)
            cov = np.cov(X, y, ddof=1)[0, 1]
            var = np.var(X, ddof=1)
            self._coefficients[key] = cov / var if var > 0 else 0.0

    def _save_history(self):
        """持久化学习状态。"""
        import pandas as pd
        records = []
        for params, pnl in self.history:
            records.append({
                "params_json": json.dumps(params, ensure_ascii=False),
                "pnl_pct": pnl,
            })
        if records:
            df = pd.DataFrame(records)
            df.to_parquet(self.db_path)

    def _load_history(self):
        """加载持久化的学习状态。"""
        if not self.db_path.exists():
            return
        try:
            import pandas as pd
            df = pd.read_parquet(self.db_path)
            for _, row in df.iterrows():
                try:
                    params = json.loads(row["params_json"])
                    self.history.append((params, row["pnl_pct"]))
                    for k in params:
                        if k not in self._param_keys:
                            self._param_keys.append(k)
                except (json.JSONDecodeError, TypeError):
                    continue
            logger.debug(f"[OnlineLearn] 加载 {len(self.history)} 笔历史数据")
            self._update_coefficients()
        except Exception as e:
            logger.debug(f"[OnlineLearn] 加载失败: {e}")

    def suggest(self) -> dict:
        """基于系数方向建议参数调整。

        Returns:
            {
                "stop_loss": {"sensitivity": -0.5, "direction": "decrease", "confidence": 0.7},
                "position_size": {"sensitivity": 0.8, "direction": "increase", "confidence": 0.6},
                ...
            }
        """
        if not self._coefficients or len(self.history) < 5:
            return {"note": "样本不足 (<5笔)"}

        suggestions = {}
        for key, beta in self._coefficients.items():
            sens = round(beta, 4)
            abs_beta = abs(beta)

            direction = "stable"
            if abs_beta > 0.01:
                direction = "increase" if beta > 0 else "decrease"

            # 置信度 = 样本量校正
            n = len(self.history)
            confidence = min(0.9, n / (n + 20))

            suggestions[key] = {
                "sensitivity": sens,
                "direction": direction,
                "confidence": round(confidence, 2),
            }

        return suggestions

    def get_top_sensitive(self, top_n: int = 3) -> list[tuple[str, float, str]]:
        """返回最敏感的前 N 个参数。"""
        if not self._coefficients:
            return []

        sorted_params = sorted(
            self._coefficients.items(),
            key=lambda x: abs(x[1]),
            reverse=True,
        )
        result = []
        for k, v in sorted_params[:top_n]:
            direction = "increase" if v > 0 else "decrease"
            result.append((k, round(v, 4), direction))
        return result

    def summary(self) -> dict:
        """学习状态摘要。"""
        return {
            "n_observations": len(self.history),
            "n_params": len(self._param_keys),
            "top_sensitive": self.get_top_sensitive(3),
            "coefficients": {k: round(v, 4) for k, v in self._coefficients.items()},
        }
