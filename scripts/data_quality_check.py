#!/usr/bin/env python3
"""数据质量报告 — 检查所有数据表的覆盖率和完整性"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from sniper.data_router import DataRouter
from sniper.signals.store import SignalStore


def check_data_quality(start: str = "2019-01-01", end: str = "2026-05-13"):
    router = DataRouter()
    sig = SignalStore()
    conn = router.wh._connect()

    print("=" * 70)
    print("  数据质量报告")
    print(f"  范围: {start} ~ {end}")
    print("=" * 70)

    total_days = len(router.get_trading_dates(start, end))

    checks = [
        ("个股日线", "daily_bars",
         lambda: pd.read_sql("SELECT COUNT(*) as c, COUNT(DISTINCT symbol) as s FROM daily_bars WHERE date>=? AND date<=?", conn, params=(start, end))),
        ("行业对比", "industry_compare",
         lambda: pd.read_sql("SELECT COUNT(DISTINCT date) as c FROM industry_compare WHERE date>=? AND date<=?", conn, params=(start, end))),
        ("龙虎榜", "dragon_tiger",
         lambda: pd.read_sql("SELECT COUNT(DISTINCT date) as c FROM dragon_tiger WHERE date>=? AND date<=?", conn, params=(start, end))),
        ("北向资金", "northbound_flow",
         lambda: pd.read_sql("SELECT COUNT(DISTINCT date) as c FROM northbound_flow WHERE date>=? AND date<=?", conn, params=(start, end))),
        ("资金流向", "fund_flow",
         lambda: pd.read_sql("SELECT COUNT(DISTINCT symbol) as s, COUNT(DISTINCT date) as d, COUNT(*) as c FROM fund_flow WHERE date>=? AND date<=?", conn, params=(start, end))),
        ("强势股", "hot_stocks",
         lambda: pd.read_sql("SELECT COUNT(DISTINCT date) as c FROM hot_stocks WHERE date>=? AND date<=?", conn, params=(start, end))),
        ("季报", "quarterly_financials",
         lambda: pd.read_sql("SELECT COUNT(DISTINCT symbol) as s, COUNT(*) as c FROM quarterly_financials", conn)),
    ]

    print(f"\n{'数据表':<20} {'覆盖度':>12} {'说明':<40}")
    print("-" * 72)

    for name, table, q in checks:
        try:
            df = q()
            if df.empty:
                status, coverage, detail = "❌", "空表", ""
            elif table == "daily_bars":
                status, coverage = "✅", f"{df.iloc[0]['s']}只股票"
                detail = f"{df.iloc[0]['c']/1e6:.1f}M行"
            elif table == "fund_flow":
                d = df.iloc[0]['d']
                if d > 0:
                    status, coverage = "✅", f"{d}天({d/total_days*100:.0f}%)"
                    detail = f"{df.iloc[0]['s']}只, {df.iloc[0]['c']}行"
                else:
                    status, coverage, detail = "⚠️", "0天", "后台下载中..."
            elif table == "quarterly_financials":
                status, coverage = "✅", f"{df.iloc[0]['s']}只"
                detail = f"{df.iloc[0]['c']}行"
            else:
                d = df.iloc[0]['c'] if len(df) > 0 else 0
                pct = d/total_days*100 if total_days > 0 else 0
                if pct >= 90:
                    status, coverage = "✅", f"{d}天({pct:.0f}%)"
                elif pct >= 10:
                    status, coverage = "⚠️", f"{d}天({pct:.0f}%)"
                else:
                    status, coverage = "❌", f"{d}天({pct:.0f}%)"
                detail = ""

            print(f"{status:<2} {name:<18} {coverage:>12} {detail:<40}")
        except Exception as e:
            print(f"❌ {name:<18} {'ERR':>12} {str(e):<40}")

    conn.close()
    print()


if __name__ == "__main__":
    check_data_quality()
