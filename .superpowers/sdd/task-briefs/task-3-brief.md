# Task 3: Warehouse intraday_snapshot CRUD

**Files:**
- Modify: `data/local/warehouse.py` — 在 LocalDataWarehouse 类末尾追加 3 个方法
- Test: `tests/test_warehouse.py` (已有文件，追加测试)

## What to do

### 1. Add methods to warehouse.py

Append these 3 methods to the `LocalDataWarehouse` class (before the closing, at the very end of the class, after `update_valuation`):

```python
# ── intraday_snapshot 盘中快照 ──

def store_intraday_snapshot(self, df: pd.DataFrame):
    """写入盘中快照。
    
    df 列: symbol, date, time, price, open, high, low, pre_close,
           volume, amount, bid1~5, ask1~5, bid_vol1~5, ask_vol1~5, source
    """
    if df.empty:
        return
    conn = self._connect()
    try:
        cols = ", ".join(f'"{c}"' for c in df.columns)
        ph = ", ".join("?" for _ in df.columns)
        data = [tuple(row) for row in df[df.columns].to_numpy()]
        conn.executemany(
            f"INSERT OR REPLACE INTO intraday_snapshot ({cols}) VALUES ({ph})",
            data,
        )
        conn.commit()
        logger.info(f"盘中快照写入: {len(df)} 行")
    finally:
        conn.close()

def get_intraday_snapshot(self, symbol: str, date: str) -> pd.DataFrame:
    """获取指定股票当日盘中快照"""
    conn = self._connect()
    try:
        df = pd.read_sql(
            "SELECT * FROM intraday_snapshot WHERE symbol=? AND date=? ORDER BY time",
            conn, params=(symbol, date),
        )
        return df if not df.empty else pd.DataFrame()
    finally:
        conn.close()

def get_intraday_snapshot_by_date(self, date: str) -> pd.DataFrame:
    """获取指定日期全部股票的盘中快照（用于持仓估值）"""
    conn = self._connect()
    try:
        df = pd.read_sql(
            "SELECT symbol, price, volume, amount, time FROM intraday_snapshot "
            "WHERE date=? ORDER BY symbol",
            conn, params=(date,),
        )
        return df if not df.empty else pd.DataFrame()
    finally:
        conn.close()
```

### 2. Add test

Append to `tests/test_warehouse.py`:

```python
def test_intraday_snapshot():
    from data.local.warehouse import LocalDataWarehouse
    import pandas as pd
    
    wh = LocalDataWarehouse()
    df = pd.DataFrame([{
        "symbol": "000001", "date": "2026-06-26", "time": "11:30:00.000",
        "price": 10.42, "open": 10.47, "high": 10.59, "low": 10.41,
        "pre_close": 10.51, "volume": 10839.99, "amount": 1136742144.0,
        "bid1": 10.42, "ask1": 10.43, "source": "mootdx",
    }])
    wh.store_intraday_snapshot(df)
    
    result = wh.get_intraday_snapshot("000001", "2026-06-26")
    assert not result.empty
    assert result["price"].iloc[0] == 10.42
    
    all_snap = wh.get_intraday_snapshot_by_date("2026-06-26")
    assert not all_snap.empty
    assert "symbol" in all_snap.columns
```

### 3. Run test

```bash
cd /d/projects/quant-system && python -m pytest tests/test_warehouse.py::test_intraday_snapshot -v
```

Expected: PASSED

### 4. Commit

```bash
git add data/local/warehouse.py tests/test_warehouse.py
git commit -m "feat(warehouse): add intraday_snapshot CRUD methods"
```
