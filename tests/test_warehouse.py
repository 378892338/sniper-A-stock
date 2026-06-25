"""Tests for LocalDataWarehouse intraday_snapshot CRUD."""


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
