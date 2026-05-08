"""本地文件缓存 — 基于 parquet，支持 TTL"""

import hashlib
import json
import time
from pathlib import Path

import pandas as pd

from config.settings import DATA_DIR
from core.logger import get_logger

logger = get_logger("shared.cache")

CACHE_DIR = Path(DATA_DIR) / "_cache"


def _cache_key(*args, **kwargs) -> str:
    raw = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cache_path(name: str, key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}_{key}.parquet"


def read_cache(name: str, *args, ttl_seconds: int = 3600, **kwargs) -> pd.DataFrame | None:
    """读取缓存，TTL过期返回None"""
    key = _cache_key(name, *args, **kwargs)
    path = cache_path(name, key)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_seconds:
        logger.debug(f"缓存过期: {path.name} (age={age:.0f}s, ttl={ttl_seconds}s)")
        return None
    try:
        return pd.read_parquet(path)
    except Exception as e:
        logger.warning(f"缓存读取失败: {path.name} — {e}")
        return None


def write_cache(df: pd.DataFrame, name: str, *args, **kwargs) -> Path:
    """写入缓存"""
    key = _cache_key(name, *args, **kwargs)
    path = cache_path(name, key)
    df.to_parquet(path, index=True)
    return path


def cache_dataframe(name: str, ttl_seconds: int = 3600):
    """DataFrame缓存装饰器 — 给数据获取函数用"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            force_refresh = kwargs.pop("force_refresh", False)
            if not force_refresh:
                cached = read_cache(name, *args, ttl_seconds=ttl_seconds, **kwargs)
                if cached is not None and not cached.empty:
                    return cached
            df = func(*args, **kwargs)
            if df is not None and not df.empty:
                write_cache(df, name, *args, **kwargs)
            return df
        return wrapper
    return decorator


def clear_expired(max_age_days: int = 7):
    """清理过期缓存文件"""
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in CACHE_DIR.glob("*.parquet"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        logger.info(f"清理过期缓存: {removed} 个文件")
