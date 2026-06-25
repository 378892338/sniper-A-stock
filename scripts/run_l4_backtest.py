"""L4 狙击手 五层架构回测入口

L0: 市场状态择时（牛/震荡/熊）
L1: 申万一级行业三维评分（动量40%+资金35%+广度25%）→ Top20%
L2: 8因子个股评分（价值/质量/情绪/风险）→ 每板块Top3-5
L3: 融合入场（MA多头排列 + 加速度 + 动量 + 快速MACD金叉 + 量能确认）
L4: 财务排雷（ST/商誉/ROE/现金流/负债率）+ ATR止损 + 移动止损 + 每日亏损上限

用法:
  python scripts/run_l4_backtest.py --n-stocks 500
  python scripts/run_l4_backtest.py --n-stocks 500 --rebuild-cache
  python scripts/run_l4_backtest.py --n-stocks 200 --repair-volume
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

import random

random.seed(42)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.l4_fractal_backtest import run_l4_backtest, print_l4_report, MAX_LOSS_PER_DAY
from core.logger import get_logger

logger = get_logger("scripts.run_l4_backtest")

CACHE_DIR = PROJECT_ROOT / "data/raw/_cache/backtest"
START = "2019-01-01"
END = "2026-05-08"


def build_sector_stock_map(
    store,
    industry_map: dict[str, str] | None = None,
    concept_map: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """构建 行业/概念板块 → 股票列表 映射。

    优先使用行业映射（industry_map），EM 行业名自动映射到申万一级行业名,
    确保与 SW_INDEX_MAP 命名一致，使大多数板块有真实指数数据。
    若行业映射不可用（如 EM 接口失败），则回退到概念板块映射。
    """
    from data.industry import SW_INDEX_MAP, EM_SW_NAME_MAP

    sector_map: dict[str, list[str]] = {}

    # ── 优先使用行业映射（EM行业名→SW一级行业名）──
    if industry_map:
        sw_names = set(SW_INDEX_MAP.values())
        for sym, industry in industry_map.items():
            if store.get_daily(sym) is None:
                continue
            # EM行业名 → SW一级行业名
            sw_name = EM_SW_NAME_MAP.get(industry, industry)
            # 只保留 SW 一级行业（有真实指数数据）
            if sw_name in sw_names:
                if sw_name not in sector_map:
                    sector_map[sw_name] = []
                sector_map[sw_name].append(sym)
        logger.info(f"行业板块映射(SW一级): {len(sector_map)} 个行业")

    # ── 行业映射不可用或过少，补充概念板块 ──
    if not sector_map or (len(sector_map) < 8 and concept_map):
        concept_added = 0
        for sym, concepts in concept_map.items():
            if store.get_daily(sym) is None:
                continue
            for concept in concepts:
                if concept not in sector_map:
                    sector_map[concept] = []
                    concept_added += 1
                sector_map[concept].append(sym)
        if concept_added > 0:
            logger.info(f"概念板块补充: {concept_added} 个概念")

    # 过滤掉成分股过少的板块（少于3只）
    before = len(sector_map)
    sector_map = {k: v for k, v in sector_map.items() if len(v) >= 3}
    logger.info(f"板块过滤: {before} → {len(sector_map)} (去掉成分股<3的板块)")

    for sec, stocks in sorted(sector_map.items(), key=lambda x: -len(x[1]))[:20]:
        logger.info(f"  板块[{sec}]: {len(stocks)} 只股票")
    if len(sector_map) > 20:
        logger.info(f"  ... 共 {len(sector_map)} 个板块")

    return sector_map


def load_industry_concept_maps() -> tuple[dict, dict]:
    """加载行业板块映射 — 仅从本地缓存，禁止在线取数。

    行业映射(sw_industry_cons_*.parquet)由 data/industry.py 的
    fetch_industry_members 预先缓存到本地。
    概念板块非必须，无缓存时返回空字典。
    """
    industry_map: dict[str, str] = {}
    concept_map: dict[str, list[str]] = {}

    shared_cache = PROJECT_ROOT / "data/raw/_cache"

    # ── 行业映射：从本地 parquet 缓存读取（跳过 TTL，数据本身仍有效）──
    ind_caches = list(shared_cache.glob("sw_industry_cons_*.parquet"))
    if ind_caches:
        try:
            ind_df = pd.read_parquet(ind_caches[0])
            if not ind_df.empty and "symbol" in ind_df.columns and "industry" in ind_df.columns:
                industry_map = dict(zip(ind_df["symbol"], ind_df["industry"]))
                logger.info(f"  行业映射(缓存): {len(industry_map)} 只股票")
        except Exception as e:
            logger.warning(f"行业缓存读取失败: {e}")

    if not industry_map:
        logger.warning("行业映射本地缓存不可用，将仅使用概念板块映射")

    # ── 概念板块：有缓存就用，没有也不拉在线 ──
    con_caches = list(shared_cache.glob("sw_concept_cons_*.parquet"))
    if con_caches:
        try:
            con_df = pd.read_parquet(con_caches[0])
            if not con_df.empty:
                cols = list(con_df.columns)
                sym_col = next((c for c in cols if c in ("symbol", "股票代码", "代码")), None)
                con_col = next((c for c in cols if c in ("concept", "板块名称", "概念")), None)
                if sym_col and con_col:
                    for _, row in con_df.iterrows():
                        sym = str(row[sym_col])
                        conc = str(row[con_col])
                        if sym not in concept_map:
                            concept_map[sym] = []
                        concept_map[sym].append(conc)
                    logger.info(f"  概念映射(缓存): {len(concept_map)} 只股票")
        except Exception as e:
            logger.warning(f"概念缓存读取失败: {e}")

    if not concept_map:
        logger.info("  概念映射本地缓存不可用，跳过（非必需）")

    return industry_map, concept_map


def rebuild_cache(n_stocks: int):
    """删除旧缓存并重新下载数据。"""
    import shutil

    # 清空个股缓存（保留指数缓存）
    logger.info("清空个股缓存...")
    for p in CACHE_DIR.glob("stock_*_daily.parquet"):
        p.unlink()
    logger.info("个股缓存已清空")

    # 重新下载
    from backtest.data_loader import fetch_stock_pool, fetch_stocks_daily

    symbols = fetch_stock_pool(max_stocks=n_stocks)
    logger.info(f"下载 {len(symbols)} 只个股日线...")
    stock_data = fetch_stocks_daily(symbols, START, END)

    for sym, df in stock_data.items():
        path = CACHE_DIR / f"stock_{sym}_daily.parquet"
        df.to_parquet(path)
    logger.info(f"个股缓存重建完成: {len(stock_data)} 只")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="L4 狙击手 五层架构回测")
    parser.add_argument("--n-stocks", type=int, default=500, help="测试股票数")
    parser.add_argument("--start", type=str, default=START)
    parser.add_argument("--end", type=str, default=END)
    parser.add_argument("--max-pool", type=int, default=10, help="L4池最大股票数")
    parser.add_argument("--rebuild-cache", action="store_true", help="重建数据缓存")
    parser.add_argument("--repair-volume", action="store_true", help="用 Baostock 修复缺失的 volume 数据")
    parser.add_argument("--diagnose", action="store_true", help="启用诊断模式(保存全链路中间状态)")
    parser.add_argument("--quality-check", action="store_true", help="运行前数据质量校验")
    args = parser.parse_args()

    # ── 缓存管理 ──
    if args.rebuild_cache:
        print("重建数据缓存...")
        rebuild_cache(args.n_stocks)

    if args.repair_volume:
        print("修复个股 volume 数据 (Baostock)...")
        from backtest.l4_fractal_backtest import repair_volume_from_baostock
        repair_volume_from_baostock(CACHE_DIR, max_stocks=args.n_stocks)

    # ── 数据质量校验（前置门控）──
    if args.quality_check:
        print("数据质量校验...")
        from data.quality import validate_warehouse
        from data.local.warehouse import LocalDataWarehouse
        wh = LocalDataWarehouse()
        reports = validate_warehouse(wh, start=args.start, end=args.end)
        n_fail = sum(1 for r in reports if not r.passed)
        if n_fail > 0:
            logger.warning(f"数据校验: {n_fail}/{len(reports)} 项未通过")
        print(f"数据校验完成: {len(reports)} 项检查, {n_fail} 项告警")

    # ── 加载数据 ──
    print(f"加载数据 (n_stocks={args.n_stocks})...")
    from backtest.data_loader import load_all_from_cache
    store = load_all_from_cache(CACHE_DIR, n_stocks=args.n_stocks)
    n_stocks = len(store.stock_names)
    logger.info(f"加载: {len(store.index_names)} 指数, {n_stocks} 个股")
    print(f"已加载 {len(store.index_names)} 指数, {n_stocks} 个股")

    # ── 行业/概念映射 ──
    print("加载行业概念映射...")
    industry_map, concept_map = load_industry_concept_maps()
    sector_stock_map = build_sector_stock_map(store, industry_map, concept_map)

    if not any(sector_stock_map.values()):
        logger.error("板块-股票映射为空，无法继续")
        return

    # ── 运行回测 ──
    print(f"\n运行 L4 狙击手回测 {args.start} → {args.end} ...")
    print(f"L0: 市场状态择时(牛/震荡/熊) | L1: 三维板块评分(动量40%+资金35%+广度25%)")
    print(f"L2: 8因子个股评分 | L3: MA250不下行(硬性)+五条件4/5(MA20>MA60+close>MA20+不追高5%+DIF>0+vol≥MA44)")
    print(f"L4: 三层退出(L0熊市+硬止损-5%+前日最低价移动止损)+组合熔断(回撤3.5%停5天)")
    print(f"L4池上限: {args.max_pool}")
    print()

    # 随机打乱避免按代码序的系统性偏见
    all_symbols = list(store.stock_names)
    random.shuffle(all_symbols)
    symbols = all_symbols[:args.n_stocks]

    # ── 诊断模式：保存到 parquet ──
    diagnose_dir = str(CACHE_DIR / "diagnose") if args.diagnose else None

    result = run_l4_backtest(
        store=store,
        symbols=symbols,
        sector_stock_map=sector_stock_map,
        start=args.start,
        end=args.end,
        max_stocks=args.max_pool,
        cache_dir=CACHE_DIR,
        diagnose_dir=diagnose_dir,
    )

    if args.diagnose:
        print(f"诊断数据已保存到 {diagnose_dir}")
        print(f"  用 diagnose_run.py query 命令查看分析结果")

    print_l4_report(result)

    return result


if __name__ == "__main__":
    main()
