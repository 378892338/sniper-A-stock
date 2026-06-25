"""回测结果缓存 — 参数哈希 + 持久化存储"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from core.logger import get_logger

logger = get_logger("sniper.engine.backtest_cache")

CACHE_DIR = Path(__file__).resolve().parents[2] / "outputs" / "backtest_cache"
RESULT_INDEX = CACHE_DIR / "result_index.parquet"


def _params_hash(params: dict) -> str:
    """参数组生成唯一哈希。"""
    params_str = json.dumps(params, sort_keys=True)
    return hashlib.md5(params_str.encode()).hexdigest()[:8]


def _cache_key(start_date: str, end_date: str, params: dict | None = None) -> str:
    """生成缓存键。"""
    parts = [start_date, end_date]
    if params:
        parts.append(_params_hash(params))
    return "_".join(parts).replace("-", "")


class BacktestCache:
    """回测结果持久化缓存。

    cache 格式:
      outputs/backtest_cache/
        result_{key}.parquet  — 回测结果 (daily_values, trades, logs)
        result_index.parquet  — 索引表 (key → sharpe, return, drawdown, params)
    """

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> dict | None:
        """从缓存读取回测结果。"""
        path = CACHE_DIR / f"result_{key}.parquet"
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            result = df.to_dict(orient="list")
            # 恢复标量
            for k in ("final_capital", "total_return"):
                if k in result and isinstance(result[k], list) and len(result[k]) > 0:
                    result[k] = result[k][0]

            # 恢复 trades 和 logs（JSON字符串→列表）
            for col in ("trades", "logs"):
                if col in result and isinstance(result[col], list) and len(result[col]) > 0:
                    try:
                        result[col] = json.loads(result[col][0])
                    except (json.JSONDecodeError, TypeError):
                        pass

            logger.info(f"[缓存] 命中: {key}")
            return result
        except Exception as e:
            logger.debug(f"[缓存] 读取失败: {e}")
            return None

    def save(self, key: str, result: dict, params: dict | None = None):
        """保存回测结果到缓存。"""
        path = CACHE_DIR / f"result_{key}.parquet"
        try:
            # 处理嵌套数据结构（只能存 JSON 可序列化类型）
            save_dict = {}
            for k, v in result.items():
                if isinstance(v, list):
                    try:
                        save_dict[k] = [json.dumps(v, ensure_ascii=False)]
                    except (TypeError, ValueError):
                        save_dict[k] = [str(v)]
                elif isinstance(v, dict):
                    try:
                        save_dict[k] = [json.dumps(v, ensure_ascii=False)]
                    except (TypeError, ValueError):
                        save_dict[k] = [str(v)]
                else:
                    save_dict[k] = [v]

            df = pd.DataFrame(save_dict)
            df.to_parquet(path)
            logger.info(f"[缓存] 保存: {key}")

            # 写入索引
            self._update_index(key, result, params)
        except Exception as e:
            logger.warning(f"[缓存] 保存失败: {e}")

    def _update_index(self, key: str, result: dict, params: dict | None = None):
        """更新结果索引表。"""
        index_path = RESULT_INDEX
        idx_data = {
            "key": [key],
            "total_return": [result.get("total_return", 0)],
            "sharpe": [result.get("sharpe", 0)],
            "max_drawdown": [result.get("max_drawdown", 0)],
            "params_hash": [_params_hash(params) if params else ""],
            "params_json": [json.dumps(params, ensure_ascii=False) if params else ""],
        }

        new_idx = pd.DataFrame(idx_data)

        if RESULT_INDEX.exists():
            try:
                old_idx = pd.read_parquet(index_path)
                # 去重（用 key）
                old_idx = old_idx[old_idx["key"] != key]
                combined = pd.concat([old_idx, new_idx], ignore_index=True)
                combined.to_parquet(index_path)
                return
            except Exception:
                pass

        new_idx.to_parquet(index_path)
        logger.info(f"[缓存] 索引更新: {key}")

    def get_index(self) -> pd.DataFrame:
        """获取所有缓存结果的索引。"""
        if not RESULT_INDEX.exists():
            return pd.DataFrame()
        try:
            return pd.read_parquet(RESULT_INDEX)
        except Exception:
            return pd.DataFrame()

    def clear(self):
        """清除所有缓存（使用前需确认）。"""
        import shutil
        shutil.rmtree(CACHE_DIR)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.warning("[缓存] 已清除")

    def prune(self, keep_top_n: int = 100):
        """只保留 Top N 结果，清除其余。"""
        idx = self.get_index()
        if idx.empty:
            return
        # 按 sharpe 排序
        idx = idx.sort_values("sharpe", ascending=False)
        keep_keys = set(idx.head(keep_top_n)["key"].tolist())

        for f in CACHE_DIR.glob("result_*.parquet"):
            key = f.stem.replace("result_", "")
            if key not in keep_keys:
                f.unlink()
                logger.info(f"[缓存] 修剪: {key}")

        pd.DataFrame().to_parquet(RESULT_INDEX)  # 清空索引重建
        for _, row in idx.iterrows():
            if row["key"] in keep_keys:
                self._update_index(row["key"], {}, json.loads(row["params_json"]) if row["params_json"] else None)

        logger.info(f"[缓存] 修剪完成: 保留 {len(keep_keys)} 条")
