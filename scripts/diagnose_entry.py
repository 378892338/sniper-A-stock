"""诊断入口条件：统计每个筛选步骤过滤了多少只股票"""

import sys
from pathlib import Path

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.data_loader import load_all_from_cache
from scripts.run_l4_backtest import load_industry_concept_maps, build_sector_stock_map
from core.logger import get_logger

logger = get_logger("scripts.diagnose_entry")

CACHE_DIR = PROJECT_ROOT / "data/raw/_cache/backtest"
START = "2019-01-01"
END = "2026-05-08"

from backtest.l4_fractal_backtest import (
    precompute_market_state,
    precompute_sector_momentum,
    precompute_sector_breadth,
    precompute_stock_factors,
    _precompute_weekly_pools,
    precompute_macd_signals,
    rank_sectors,
    screen_stocks_v2,
    MAX_HOLDINGS,
    VOLUME_BREAKOUT_RATIO,
)

def diagnose():
    store = load_all_from_cache(CACHE_DIR, n_stocks=200)
    symbols = store.stock_names[:200]
    print(f"已加载 {len(store.index_names)} 指数, {len(symbols)} 个股")

    industry_map, concept_map = load_industry_concept_maps()
    sector_stock_map = build_sector_stock_map(store, industry_map, concept_map)

    # 构建 filtered_sector_map
    test_symbols = set(symbols)
    filtered_sector_map = {}
    for sec, ss in sector_stock_map.items():
        filtered = [s for s in ss if s in test_symbols]
        if filtered:
            filtered_sector_map[sec] = filtered

    all_candidate_symbols = set(test_symbols)
    for ss in filtered_sector_map.values():
        all_candidate_symbols.update(ss)
    candidate_list = list(all_candidate_symbols)

    print(f"测试股票: {len(symbols)}, 板块: {len(filtered_sector_map)}, 候选: {len(candidate_list)}")
    for sec, ss in sorted(filtered_sector_map.items(), key=lambda x: -len(x[1]))[:10]:
        print(f"  {sec}: {len(ss)} 只")

    biz_days = pd.bdate_range(START, END)
    print(f"交易日: {len(biz_days)} 天")

    # L0
    market_state = precompute_market_state(store, biz_days)
    state_counts = market_state.value_counts()
    print(f"市场状态: {dict(state_counts)}")

    # 获取申万指数
    from backtest.l4_fractal_backtest import fetch_sw_sector_indices, _synthesize_concept_prices
    sector_names = list(filtered_sector_map.keys())
    sw_prices = fetch_sw_sector_indices(
        CACHE_DIR,
        sector_names=sector_names,
        start_date=START.replace("-", ""),
        end_date=END.replace("-", ""),
    )
    sector_prices = {}
    for name in sector_names:
        if name in sw_prices:
            sector_prices[name] = sw_prices[name]
    print(f"申万指数匹配: {len(sector_prices)}/{len(sector_names)}")

    # 预计算板块动量/广度/因子
    sector_momentum = precompute_sector_momentum(sector_names, sector_prices, biz_days)
    sector_breadth = precompute_sector_breadth(filtered_sector_map, store, biz_days)
    factor_data = precompute_stock_factors(store, candidate_list)
    macd_signals = precompute_macd_signals(store, candidate_list)

    print(f"\n板块动量: {len(sector_momentum)} 个板块")
    print(f"板块广度: {len(sector_breadth)} 个板块")
    print(f"因子数据: {len(factor_data)} 只股票")
    print(f"MACD信号: {len(macd_signals)} 只股票")

    # ── 诊断：选一个 bull 周五，逐条件检查 ──
    bull_fridays = []
    for d in biz_days:
        if d.weekday() == 4 and market_state.get(d) == "bull":
            bull_fridays.append(d)
    print(f"\nBull 周五数量: {len(bull_fridays)}")

    if not bull_fridays:
        # 找第一个任意非bear的周五
        for d in biz_days:
            if d.weekday() == 4 and market_state.get(d) in ("bull", "volatile"):
                bull_fridays.append(d)
                break
        print(f"无bull周五，使用第一个非bear周五")

    if bull_fridays:
        test_date = bull_fridays[0]
        wk = test_date.strftime("%Y-%m-%d")
        print(f"\n{'='*60}")
        print(f"诊断日: {test_date} (周{test_date.weekday()})")
        print(f"市场状态: {market_state.get(test_date, 'N/A')}")

        # 板块排名
        top_sectors = rank_sectors(
            store, filtered_sector_map, test_date,
            sector_momentum=sector_momentum,
            sector_breadth_scores=sector_breadth,
        )
        strong_sectors = [s for s, _ in top_sectors]
        print(f"强势板块 ({len(strong_sectors)}): {strong_sectors}")

        # L4 pool
        scored = screen_stocks_v2(
            list(candidate_list), factor_data, test_date,
            max_stocks=MAX_HOLDINGS * 5,
        )
        pool_stocks = [s for s, _, _ in scored]
        print(f"L4池大小: {len(pool_stocks)}")

        # 预构建 stock→sector 映射
        stock_to_sector = {}
        for sec, ss in filtered_sector_map.items():
            for s in ss:
                stock_to_sector[s] = sec

        # 逐条件检查
        total_checked = 0
        cond_ma20_pass = 0
        cond_ma250_pass = 0
        cond_confirmed = 0
        cond_sector_match = 0

        for sym in pool_stocks:
            total_checked += 1
            df_stock = store.get_daily(sym)
            if df_stock is None:
                continue

            ds_str = str(test_date)[:10]
            if ds_str not in df_stock.index:
                continue

            s_close = float(df_stock.loc[ds_str, "close"])

            # MA20 check
            c = df_stock["close"]
            ma20 = c.rolling(20).mean()
            if ds_str not in ma20.index or pd.isna(ma20.loc[ds_str]):
                continue
            if not (s_close > ma20.loc[ds_str]):
                continue
            cond_ma20_pass += 1

            # 年线 check (MA250 not downward: current > 20-day-ago)
            if len(c) > 250:
                ma250 = c.rolling(250).mean()
                ma250 = ma250[ma250.index <= ds_str]
                if len(ma250) >= 20:
                    ma250_now = float(ma250.iloc[-1])
                    ma250_prev = float(ma250.iloc[-20])
                    if not pd.isna(ma250_now) and not pd.isna(ma250_prev) and ma250_now < ma250_prev:
                        continue
            cond_ma250_pass += 1

            # 4 signals
            ms = macd_signals.get(sym)
            if ms is None:
                continue
            confirm = 0

            if ds_str in ms["ret_3m"].index and ds_str in ms["ret_6m"].index:
                r3 = float(ms["ret_3m"].loc[ds_str])
                r6 = float(ms["ret_6m"].loc[ds_str])
                if not pd.isna(r3) and not pd.isna(r6) and r3 > r6:
                    confirm += 1
            if ds_str in ms["ret_1m"].index:
                r1 = float(ms["ret_1m"].loc[ds_str])
                if not pd.isna(r1) and r1 > 0:
                    confirm += 1
            if "fast_golden_cross" in ms and ds_str in ms["fast_golden_cross"].index:
                if bool(ms["fast_golden_cross"].loc[ds_str]):
                    confirm += 1
            if ds_str in ms["volume_ma"].index:
                s_vol = float(df_stock.loc[ds_str, "volume"])
                vol_ma = float(ms["volume_ma"].loc[ds_str])
                if not pd.isna(vol_ma) and s_vol >= vol_ma * VOLUME_BREAKOUT_RATIO:
                    confirm += 1

            if confirm < 2:
                continue
            cond_confirmed += 1

            # 板块匹配
            sec = stock_to_sector.get(sym, "")
            if sec in strong_sectors:
                cond_sector_match += 1

        print(f"\n逐条件通过率 (池中{total_checked}只):")
        print(f"  ① close > MA20:     {cond_ma20_pass}/{total_checked} ({cond_ma20_pass/total_checked*100:.0f}%)")
        print(f"  ② 年线不向下:       {cond_ma250_pass}/{total_checked} ({cond_ma250_pass/total_checked*100:.0f}%)")
        print(f"  ③ 4选2确认信号:     {cond_confirmed}/{total_checked} ({cond_confirmed/total_checked*100:.0f}%)")
        print(f"  ④ 在强势板块:       {cond_sector_match}/{total_checked} ({cond_sector_match/total_checked*100:.0f}%)")

        # 各个信号分布
        print(f"\n  各信号触发率 (池中{total_checked}只):")
        acc = ret1 = gc = vol = 0
        for sym in pool_stocks:
            ms = macd_signals.get(sym)
            if ms is None:
                continue
            ds_str = str(test_date)[:10]
            if ds_str in ms["ret_3m"].index and ds_str in ms["ret_6m"].index:
                r3 = float(ms["ret_3m"].loc[ds_str])
                r6 = float(ms["ret_6m"].loc[ds_str])
                if not pd.isna(r3) and not pd.isna(r6) and r3 > r6:
                    acc += 1
            if ds_str in ms["ret_1m"].index:
                r1 = float(ms["ret_1m"].loc[ds_str])
                if not pd.isna(r1) and r1 > 0:
                    ret1 += 1
            if "fast_golden_cross" in ms and ds_str in ms["fast_golden_cross"].index:
                if bool(ms["fast_golden_cross"].loc[ds_str]):
                    gc += 1
            if ds_str in ms["volume_ma"].index:
                df_stock = store.get_daily(sym)
                if df_stock is not None and ds_str in df_stock.index:
                    s_vol = float(df_stock.loc[ds_str, "volume"])
                    vol_ma = float(ms["volume_ma"].loc[ds_str])
                    if not pd.isna(vol_ma) and s_vol >= vol_ma * VOLUME_BREAKOUT_RATIO:
                        vol += 1
        print(f"  加速度(acceleration): {acc}/{total_checked}")
        print(f"  短期动量(ret_1m>0):  {ret1}/{total_checked}")
        print(f"  MACD金叉:             {gc}/{total_checked}")
        print(f"  量能确认:             {vol}/{total_checked}")


if __name__ == "__main__":
    diagnose()
