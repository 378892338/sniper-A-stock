"""L2 候选股缓存，避免重复计算 L2 评分"""

import json
import os
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / ".l2_cache"
CACHE_FILE = CACHE_DIR / "top_stocks.json"


def _ensure_cache():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_cache() -> dict[str, list[dict]]:
    """加载 L2 缓存 {date: [top_stocks]}"""
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict[str, list[dict]]):
    """保存 L2 缓存"""
    _ensure_cache()
    # 只写新数据，不覆盖已有
    existing = load_cache()
    existing.update(cache)
    with open(CACHE_FILE, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def get_missing_dates(dates: list[str]) -> list[str]:
    """返回需要计算的日期列表"""
    cache = load_cache()
    return [d for d in dates if d not in cache]


def save_top_stocks(date: str, top_stocks: list[dict]):
    """保存单日 L2 结果"""
    cache = load_cache()
    cache[date] = top_stocks
    # 增量保存
    _ensure_cache()
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def get_top_stocks(date: str) -> list[dict] | None:
    """获取单日 L2 结果"""
    cache = load_cache()
    return cache.get(date)


def cache_info() -> dict:
    """返回缓存统计"""
    cache = load_cache()
    dates = sorted(cache.keys())
    return {
        "total_dates": len(dates),
        "date_range": f"{dates[0]} ~ {dates[-1]}" if dates else "空",
        "dates": dates,
    }


def clear_cache():
    """清空缓存"""
    if CACHE_FILE.exists():
        os.remove(CACHE_FILE)
