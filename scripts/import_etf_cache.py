"""把本地 ETF cache parquet 批量导入 warehouse.index_daily 表

ETF cache 路径：QUANT_DATA_ROOT/raw/_cache/backtest/etf_daily_*.parquet
每个 parquet 包含一个 ETF 的历史日线，列名可能是 close/open/high/low/volume/etf_name
导入后：warehouse.index_daily 表增加记录，name 字段 = etf_name，date=日期

回测时 get_etf_daily() 走 L0 warehouse 直接命中，不再走网络。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from config.paths import QUANT_DATA_ROOT
from data.local.warehouse import LocalDataWarehouse

CACHE_DIR = QUANT_DATA_ROOT / "raw" / "_cache" / "backtest"

etf_files = sorted(CACHE_DIR.glob("etf_daily_*.parquet"))
print(f"找到 {len(etf_files)} 个 ETF cache 文件")

wh = LocalDataWarehouse()
inserted = 0
skipped = 0
for f in etf_files:
    # 提取 ETF 名称 (etf_daily_光伏.parquet → 光伏)
    etf_name = f.stem.replace("etf_daily_", "")
    try:
        df = pd.read_parquet(str(f))
        if df.empty:
            skipped += 1
            continue

        # date 在 index 而非列 → 重置
        if df.index.name == "date" or "date" in df.index.names:
            df = df.reset_index()
        elif "date" not in df.columns and "DateTime" in df.columns:
            df = df.rename(columns={"DateTime": "date"})
        if "name" not in df.columns and "etf_name" in df.columns:
            df["name"] = df["etf_name"]

        if "date" not in df.columns or "name" not in df.columns:
            print(f"  {etf_name}: 缺少必要列 {list(df.columns)}")
            skipped += 1
            continue

        # 加 name 字段（如有缺失）
        df["name"] = etf_name
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df[["date", "name", "open", "high", "low", "close", "volume"]].copy()
        # 清理 NaN
        for col in ["open", "high", "low", "close"]:
            if col in df.columns:
                df[col] = df[col].fillna(0)
        if "volume" in df.columns:
            df["volume"] = df["volume"].fillna(0)

        # 写入 warehouse
        wh.store_index_daily(df, if_exists="append")
        inserted += len(df)
        print(f"  {etf_name}: {len(df)} 行")
    except Exception as e:
        print(f"  {etf_name}: 错误 {e}")
        skipped += 1

print(f"\n汇总: 插入 {inserted} 行, 跳过 {skipped} 文件")
# 验证
from config.paths import META_DB_PATH
import sqlite3
conn = sqlite3.connect(META_DB_PATH)
c = conn.cursor()
count = c.execute('SELECT COUNT(*) FROM index_daily WHERE name IN (SELECT DISTINCT name FROM index_daily WHERE name LIKE "证券" OR name LIKE "医药" OR name LIKE "光伏" OR name LIKE "酒" OR name LIKE "新能源车" OR name LIKE "煤炭" OR name LIKE "军工" OR name LIKE "半导体" OR name LIKE "汽车" OR name LIKE "消费" OR name LIKE "科技" OR name LIKE "有色" OR name LIKE "银行")').fetchone()[0]
print(f"warehouse.index_daily 中 ETF 类记录: {count} 行")
conn.close()
