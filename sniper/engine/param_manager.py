"""参数管理器 — 存储 + 更新 + 回滚 + 漂移检测"""

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from sniper import config as cfg
from core.logger import get_logger

logger = get_logger("sniper.engine.param_manager")

PARAM_DB = Path(__file__).resolve().parents[2] / "outputs" / "params.db"

DEFAULT_PARAMS = {
    # 风控参数
    "stop_loss": -0.08,
    "trailing_stop": -0.05,
    "max_hold_days": 15,
    "position_size": 0.12,

    # 市场状态参数
    "bullish_threshold": 60.0,
    "bearish_threshold": 40.0,

    # L3 入场过滤
    "soft_min_score": 70.0,
    "soft_sector_top": 3,
}

PARAM_RANGES = {
    "stop_loss": (-0.25, -0.02),
    "trailing_stop": (-0.15, -0.01),
    "max_hold_days": (3, 90),
    "position_size": (0.03, 0.35),
    "bullish_threshold": (40, 80),
    "bearish_threshold": (20, 55),
    "soft_min_score": (40, 90),
    "soft_sector_top": (1, 10),
}


class ParamDB:
    """参数持久化存储（SQLite）。"""

    def __init__(self, db_path: str | Path = PARAM_DB):
        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS params (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS param_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    old_value REAL,
                    new_value REAL,
                    changed_at TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT 'manual',
                    reason TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS param_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    suggested_value REAL,
                    impact REAL DEFAULT 0,
                    confidence REAL DEFAULT 0,
                    suggested_at TEXT NOT NULL,
                    applied INTEGER DEFAULT 0
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def load_all(self) -> dict[str, float]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT key, value FROM params").fetchall()
            return {row["key"]: row["value"] for row in rows}
        finally:
            conn.close()

    def save(self, key: str, value: float):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO params (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
        finally:
            conn.close()

    def save_batch(self, params: dict[str, float]):
        conn = self._connect()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for key, value in params.items():
                conn.execute(
                    "INSERT OR REPLACE INTO params (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
            conn.commit()
        finally:
            conn.close()

    def log_change(self, key: str, old_value: float, new_value: float,
                   trigger: str = "manual", reason: str = ""):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO param_history (key, old_value, new_value, changed_at, trigger, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (key, old_value, new_value, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), trigger, reason),
            )
            conn.commit()
        finally:
            conn.close()

    def log_suggestion(self, key: str, suggested_value: float,
                       impact: float = 0, confidence: float = 0) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO param_suggestions (key, suggested_value, impact, confidence, suggested_at) VALUES (?, ?, ?, ?, ?)",
                (key, suggested_value, impact, confidence, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def mark_suggestion_applied(self, suggestion_id: int):
        conn = self._connect()
        try:
            conn.execute("UPDATE param_suggestions SET applied = 1 WHERE id = ?", (suggestion_id,))
            conn.commit()
        finally:
            conn.close()

    def get_history(self, key: str = "", limit: int = 100) -> list[dict]:
        conn = self._connect()
        try:
            sql = "SELECT * FROM param_history"
            params_sql: list[Any] = []
            if key:
                sql += " WHERE key = ?"
                params_sql.append(key)
            sql += " ORDER BY changed_at DESC LIMIT ?"
            params_sql.append(limit)
            rows = conn.execute(sql, params_sql).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_suggestions(self, applied: int | None = None, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            sql = "SELECT * FROM param_suggestions"
            params_sql: list[Any] = []
            if applied is not None:
                sql += " WHERE applied = ?"
                params_sql.append(applied)
            sql += " ORDER BY suggested_at DESC LIMIT ?"
            params_sql.append(limit)
            rows = conn.execute(sql, params_sql).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


class DriftDetector:
    """检测参数是否过度偏离初始值。"""

    def __init__(self, initial_params: dict | None = None,
                 threshold: float = 0.20):
        self.initial = initial_params or dict(DEFAULT_PARAMS)
        self.threshold = threshold

    def check(self, current_params: dict) -> dict:
        """检查参数漂移程度。

        Returns:
            {key: {"initial": val, "current": val, "drift_pct": float, "flag": bool}}
        """
        results = {}
        for key, init_val in self.initial.items():
            cur_val = current_params.get(key, init_val)
            if isinstance(init_val, (int, float)) and init_val != 0:
                drift = abs(cur_val - init_val) / abs(init_val)
            else:
                drift = 0.0 if cur_val == init_val else 1.0

            results[key] = {
                "initial": init_val,
                "current": cur_val,
                "drift_pct": round(drift, 4),
                "flag": drift > self.threshold,
            }
        return results

    def summary(self, current_params: dict) -> str:
        """生成漂移摘要。"""
        results = self.check(current_params)
        flagged = [k for k, v in results.items() if v["flag"]]
        if not flagged:
            return f"参数漂移检查通过 (阈值={self.threshold:.0%})"

        lines = [f"参数漂移预警 (阈值>{self.threshold:.0%}):"]
        for k in flagged:
            v = results[k]
            lines.append(f"  {k}: {v['initial']} → {v['current']} (漂移{v['drift_pct']:.1%})")
        return "\n".join(lines)


class ParamManager:
    """参数管理器：存储 + 更新 + 回滚 + 安全机制。

    核心功能:
      - 参数持久化到 SQLite
      - 应用评估器建议（需满足最小改进阈值 + 置信度）
      - 参数变更历史追溯
      - 回滚保护
      - 参数范围约束
    """

    def __init__(self, db_path: str | Path | None = None,
                 min_improvement: float = 0.01,
                 active: bool = False):
        """
        Args:
            db_path: 参数持久化路径
            min_improvement: 最小改进阈值（impact 低于此值不应用）
            active: 是否启用（为 False 时不自动应用参数变更）
        """
        self.db = ParamDB(db_path or PARAM_DB)
        self.min_improvement = min_improvement
        self.active = active
        self._current: dict[str, float] = {}
        self._change_history: list[dict] = []
        self._load_current()

    def _load_current(self):
        """加载当前参数（DB 优先，缺失的用默认值）。"""
        stored = self.db.load_all()
        self._current = dict(DEFAULT_PARAMS)
        self._current.update(stored)

    def get(self) -> dict:
        """获取当前参数快照。"""
        return deepcopy(self._current)

    def get_json(self) -> str:
        """获取 JSON 字符串（用于 TradeLog）。"""
        return json.dumps(self._current, ensure_ascii=False)

    def get_value(self, key: str) -> float:
        return self._current.get(key, DEFAULT_PARAMS.get(key, 0.0))

    def set(self, key: str, value: float, trigger: str = "manual",
            reason: str = ""):
        """设置单个参数值。"""
        # 范围约束
        if key in PARAM_RANGES:
            lo, hi = PARAM_RANGES[key]
            value = max(lo, min(hi, value))

        old = self._current.get(key)

        if old == value:
            return

        self._current[key] = value
        self.db.save(key, value)
        self.db.log_change(key, old, value, trigger, reason)
        self._change_history.append({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "key": key,
            "old": old,
            "new": value,
            "trigger": trigger,
        })
        logger.info(f"[参数] {key}: {old} → {value} ({trigger})")

        # 更新运行中配置
        self._update_runtime_config({key: value})

    def set_batch(self, params: dict[str, float], trigger: str = "manual",
                  reason: str = ""):
        """批量设置参数。"""
        clamped = {}
        for key, value in params.items():
            if key in PARAM_RANGES:
                lo, hi = PARAM_RANGES[key]
                value = max(lo, min(hi, value))
            clamped[key] = value

        self._current.update(clamped)
        self.db.save_batch(clamped)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for key, value in clamped.items():
            old = self._current.get(key)
            if old != value:
                self.db.log_change(key, old, value, trigger, reason)

        self._change_history.append({
            "date": now,
            "keys": list(clamped.keys()),
            "trigger": trigger,
        })
        logger.info(f"[参数] 批量更新 {len(clamped)} 项 ({trigger})")

        self._update_runtime_config(clamped)

    def _update_runtime_config(self, updates: dict):
        """更新运行中的配置文件（sniper.config）。"""
        exit_attrs = {"stop_loss", "trailing_stop", "max_hold_days", "ma_break_below"}
        risk_attrs = {"position_size", "max_positions", "max_daily_loss", "max_total_loss", "min_hold_days"}

        exit_updates = {k: v for k, v in updates.items() if k in exit_attrs}
        risk_updates = {k: v for k, v in updates.items() if k in risk_attrs}

        if exit_updates:
            cfg.EXIT = cfg.ExitConfig(**{**asdict(cfg.EXIT), **exit_updates})
        if risk_updates:
            cfg.RISK = cfg.RiskConfig(**{**asdict(cfg.RISK), **risk_updates})

    def apply_suggestion(self, key: str, suggestion: dict) -> bool:
        """应用评估器建议。返回是否应用成功。"""
        if not self.active:
            return False

        impact = suggestion.get("impact", 0)
        confidence = suggestion.get("confidence", 0)
        optimal = suggestion.get("optimal")
        if optimal is None:
            return False

        if impact < self.min_improvement:
            return False
        if confidence < 0.5:
            return False

        suggestion_id = self.db.log_suggestion(key, optimal, impact, confidence)
        self.set(key, optimal, trigger="auto_calibrate",
                 reason=f"影响={impact:.3f}, 置信={confidence:.2f}")
        self.db.mark_suggestion_applied(suggestion_id)
        return True

    def apply_batch_suggestions(self, suggestions: dict) -> int:
        """批量应用建议。返回成功数量。"""
        if not suggestions:
            return 0

        applied = 0
        updates = {}
        for key, s in suggestions.items():
            if key in DEFAULT_PARAMS and isinstance(s, dict):
                impact = s.get("impact", 0)
                confidence = s.get("confidence", 0)
                optimal = s.get("optimal")

                if impact >= self.min_improvement and confidence >= 0.5 and optimal is not None:
                    updates[key] = optimal
                    applied += 1

        if updates:
            self.set_batch(updates, trigger="auto_calibrate",
                           reason="批量月度校准")

        return applied

    def rollback(self, n_steps: int = 1) -> int:
        """回滚最近的参数变更。返回回滚的参数数量。"""
        if n_steps <= 0:
            return 0

        history = self.db.get_history(limit=n_steps)
        if not history:
            logger.warning("[参数] 无历史记录，无法回滚")
            return 0

        rollbacks = {}
        for h in reversed(history):
            key = h["key"]
            old_val = h["old_value"]
            if key not in rollbacks:
                rollbacks[key] = old_val

        self.set_batch(rollbacks, trigger="rollback",
                       reason=f"回滚 {n_steps} 步")
        logger.warning(f"[参数] 回滚 {len(rollbacks)} 项, {n_steps} 步")
        return len(rollbacks)

    def get_history(self, key: str = "", limit: int = 100) -> list[dict]:
        return self.db.get_history(key, limit)

    def get_suggestions(self, applied: int | None = None) -> list[dict]:
        return self.db.get_suggestions(applied)

    def summary(self) -> dict:
        """参数摘要（用于日报）。"""
        return {
            "params": self.get(),
            "last_change": self._change_history[-1] if self._change_history else None,
            "total_changes": len(self._change_history),
        }

    def export(self, path: str | Path | None = None) -> str:
        """导出当前参数为 JSON。"""
        path = Path(path) if path else Path("params_export.json")
        with open(path, "w") as f:
            json.dump(self.get(), f, indent=2, ensure_ascii=False)
        logger.info(f"[参数] 已导出: {path}")
        return str(path)

    def import_from(self, path: str | Path) -> dict:
        """从 JSON 导入参数。"""
        path = Path(path)
        with open(path) as f:
            params = json.load(f)
        self.set_batch(params, trigger="import", reason=f"从 {path.name} 导入")
        logger.info(f"[参数] 已导入: {path} ({len(params)} 项)")
        return params
