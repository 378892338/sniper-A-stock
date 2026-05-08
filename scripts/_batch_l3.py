"""批量重算 L3 评分 — 多进程 + 后处理截面标准化"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed
from core.logger import get_logger

logger = get_logger("batch_l3")
CACHE_DIR = Path("data/raw/_cache/backtest")


def _load_or_build_etf_map() -> dict[str, list[str]]:
    """加载或重建 ETF 标签映射（含修正编码）"""
    import time as _time
    etf_cache = CACHE_DIR / "etf_tags.parquet"

    # 尝试读缓存
    if etf_cache.exists():
        age = _time.time() - etf_cache.stat().st_mtime
        if age < 30 * 86400:
            try:
                df = pd.read_parquet(etf_cache)
                result: dict[str, list[str]] = {}
                for _, row in df.iterrows():
                    tags = row["etf_tags"]
                    if isinstance(tags, np.ndarray):
                        # numpy array 转 list，处理隐码
                        decoded = []
                        for t in tags:
                            if isinstance(t, str) and t.strip():
                                decoded.append(t.strip())
                        result[row["symbol"]] = decoded
                    elif isinstance(tags, (list, tuple)):
                        result[row["symbol"]] = [t for t in tags if t]
                    else:
                        result[row["symbol"]] = []
                non_empty = sum(1 for v in result.values() if v)
                if non_empty > 0:
                    logger.info(f"ETF 标签加载完成: {non_empty}/{len(result)} 有归属")
                    return result
                else:
                    logger.warning("ETF 标签缓存为空，需重建")
            except Exception as e:
                logger.warning(f"ETF 缓存读取失败: {e}")

    # 重建
    logger.info("重建 ETF 标签映射...")
    try:
        from gate.sector_mapper import SectorMapper
        from data.industry import build_symbol_industry_map, build_symbol_concepts_map

        mapper = SectorMapper()
        industry_map = build_symbol_industry_map()
        concept_map = build_symbol_concepts_map()

        # 获取所有股票代码（从文件名）
        files = sorted(CACHE_DIR.glob("stock_*_daily.parquet"))
        all_symbols = [f.stem.replace("stock_", "").replace("_daily", "") for f in files]

        result = {}
        for symbol in all_symbols:
            industry = industry_map.get(symbol)
            concepts = concept_map.get(symbol, [])
            tags = mapper.map_stock_to_etf(
                symbol_industry=industry,
                symbol_concepts=concepts if concepts else None,
            )
            result[symbol] = list(tags) if tags else []

        # 持久化
        rows = [{"symbol": s, "etf_tags": t} for s, t in result.items()]
        pd.DataFrame(rows).to_parquet(etf_cache, index=False)
        non_empty = sum(1 for v in result.values() if v)
        logger.info(f"ETF 标签重建完成: {non_empty}/{len(result)} 有归属")
        return result
    except Exception as e:
        logger.warning(f"ETF 标签重建失败: {e}")
        return {}


def _assess_one(sym: str) -> dict | None:
    """多进程包装：加载全量日线，重采样周线，用全量数据评估"""
    try:
        import sys as _sys
        _sys.path.insert(0, str(CACHE_DIR.parent.parent.parent))
        from gate.layer3_stock import assess_stock
        import pandas as pd

        df = pd.read_parquet(CACHE_DIR / f"stock_{sym}_daily.parquet")
        if df is None or len(df) < 200:
            return None

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        # 全量周线
        weekly = df.resample("W").agg(
            {"open": "first", "close": "last", "high": "max", "low": "min", "volume": "sum"}
        ).dropna()

        # 最新月份（仅用于标记）
        latest_month = df.resample("ME").groups
        if not latest_month:
            return None
        month_label = list(latest_month.keys())[-1].strftime("%Y-%m")

        # 传全量日线 + 全量周线 → MACD 基于全序列，指标稳定
        # 不传月线（退化评分只能跳过多项月线检查，但节省 90% 时间）
        v = assess_stock(sym, daily_df=df, weekly_df=weekly)
        return {
            "symbol": sym, "month": month_label,
            "passed": v.passed_gate, "score": v.score,
            "classification": v.classification,
            "pattern_mult": v.pattern_mult,
        }
    except Exception:
        return None


def _normalize_scores(result_df: pd.DataFrame) -> pd.DataFrame:
    """对评分做截面标准化，增强区分度"""
    df = result_df.copy()
    passed_mask = df["passed"].values
    passed_idx = np.where(passed_mask)[0]

    if len(passed_idx) < 10:
        return result_df

    scores = df["score"].values[passed_idx].astype(float)
    mean_s = np.mean(scores)
    std_s = max(np.std(scores), 5.0)

    # Z-score → 映射到 30-90 范围
    z = (scores - mean_s) / std_s
    scaled = np.clip(50 + z * 10, 30, 90).round(1)

    df.loc[passed_mask, "score"] = scaled
    df.loc[passed_mask, "classification"] = np.where(scaled >= 50, "强势", "一般")

    logger.info(f"标准化后分数: {df.loc[passed_mask, 'score'].min():.1f} ~ "
                f"{df.loc[passed_mask, 'score'].max():.1f}, "
                f"均值 {df.loc[passed_mask, 'score'].mean():.2f}")
    return df


def main():
    t0 = time.time()

    # 收集所有股票代码
    files = sorted(CACHE_DIR.glob("stock_*_daily.parquet"))
    symbols = [f.stem.replace("stock_", "").replace("_daily", "") for f in files]
    logger.info(f"加载 {len(symbols)} 只股票")

    # ── 构建 ETF 标签映射 ──
    etf_map = _load_or_build_etf_map()
    logger.info(f"ETF 映射: {sum(1 for v in etf_map.values() if v)}/{len(etf_map)} 有归属")

    # ── 多进程评估 ──
    rows = []
    with ProcessPoolExecutor(max_workers=8) as executor:
        fut_map = {executor.submit(_assess_one, sym): sym for sym in symbols}
        done = 0
        for fut in as_completed(fut_map):
            done += 1
            if done % 500 == 0:
                logger.info(f"  {done}/{len(symbols)} ({time.time()-t0:.0f}s)")
            result = fut.result()
            if result is not None:
                # 填充 etf_tags
                sym = result["symbol"]
                result["etf_tags"] = etf_map.get(sym, [])
                rows.append(result)

    raw_df = pd.DataFrame(rows)
    logger.info(f"原始评估: {len(raw_df)} 条, {time.time()-t0:.0f}s")

    # ── 截面标准化 ──
    result_df = _normalize_scores(raw_df)

    # ── 保存 ──
    result_df.to_parquet(CACHE_DIR / "l3_scores_all.parquet", index=False)
    elapsed = time.time() - t0
    logger.info(f"保存完成, 总计 {elapsed:.0f}s")

    # ── 报告 ──
    print(f"\nL3 重算完成: {len(result_df)} 条, {result_df['symbol'].nunique()} 只股票")
    print(f"耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    passed = result_df[result_df["passed"]]
    print(f"通过: {len(passed)}/{len(result_df)} ({len(passed)/len(result_df)*100:.0f}%)")

    # 板块分布
    board_info = {}
    for _, r in result_df.iterrows():
        b = r["symbol"][:3]
        if b not in board_info:
            board_info[b] = {"total": 0, "passed": 0}
        board_info[b]["total"] += 1
        if r["passed"]:
            board_info[b]["passed"] += 1
    for b in sorted(board_info):
        info = board_info[b]
        print(f"  {b}: {info['total']}只, 通过{info['passed']}只")

    top = passed.nlargest(15, "score")
    print(f"\nTop 15 (最新月份 {result_df['month'].iloc[0]}):")
    for _, r in top.iterrows():
        tags = ";".join(r["etf_tags"]) if isinstance(r.get("etf_tags"), list) and r["etf_tags"] else "-"
        print(f"  {r['symbol']} | score={r['score']:.1f} | {r['classification']} | [{tags}]")

    # 按板块展示通过率
    print("\n创业板通过股票 (部分):")
    cyb = passed[passed["symbol"].str.match(r"^30\d{4}")].nlargest(5, "score")
    for _, r in cyb.iterrows():
        tags = ";".join(r["etf_tags"]) if isinstance(r.get("etf_tags"), list) and r["etf_tags"] else "-"
        print(f"  {r['symbol']} | score={r['score']:.1f} | {r['classification']} | [{tags}]")


if __name__ == "__main__":
    main()
