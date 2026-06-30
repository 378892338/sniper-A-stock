"""从日线数据计算强势股（涨停检测）

数据真实性说明:
  - 涨停检测：从 daily_bars 的 close pct_change 检测，>= 阈值即为涨停，数据真实
  - 资金流向：东方财富 API 只返回近6个月，历史数据无可获取的免费源，不做伪造
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import numpy as np
from datetime import datetime

from sniper.signals.store import SignalStore
from sniper.signals.schema import T_HOT_STOCKS, T_INDUSTRY_COMPARE
from core.logger import get_logger

logger = get_logger("sniper.signals.compute_from_bars")


def compute_hot_stocks_from_bars(store: SignalStore | None = None,
                                  start: str = "2019-01-01",
                                  end: str | None = None) -> int:
    """从 daily_bars 计算强势股（涨停检测），覆盖全部历史。

    涨停规则（真实，非 proxy）:
      主板(60/00): close pct_change >= 9.5%
      创业板(30):                     >= 19.5%
      科创板(688):                    >= 19.5%
      北交所(8):                     >= 29.5%

    为什么真实:
      daily_bars 中的 close 是交易所发布的实际成交价，
      涨跌幅是真实计算结果，涨停是可验证的客观事实。
    """
    if store is None:
        store = SignalStore()
    end = end or datetime.now().strftime("%Y-%m-%d")

    conn = store._connect()
    try:
        # ⚠️ DATA_MODIFICATION — 只删待更新日期范围，不碰全量历史
        conn.execute(f"DELETE FROM {T_HOT_STOCKS} WHERE date >= ? AND date <= ?", (start, end))
        conn.commit()

        # 加载全量日线
        logger.info("加载 daily_bars...")
        bars = pd.read_sql(
            "SELECT symbol, date, close FROM daily_bars WHERE date >= ? AND date <= ? ORDER BY symbol, date",
            conn, params=(start, end),
        )
        logger.info(f"  {len(bars)} 行, {bars['symbol'].nunique()} 只")

        # 计算涨跌幅
        bars["pct"] = bars.groupby("symbol")["close"].transform(lambda x: x.pct_change())

        # 涨停阈值
        def _limit(sym):
            if sym.startswith("30") or sym.startswith("688"):
                return 0.195
            elif sym.startswith("8"):
                return 0.295
            return 0.095

        bars["limit"] = bars["symbol"].apply(_limit)
        hot = bars[bars["pct"] >= bars["limit"]][["date", "symbol"]].copy()
        hot["reason_tags"] = "涨停"
        hot = hot.drop_duplicates(subset=["date", "symbol"])

        # 写入
        hot_records = hot.to_records(index=False)
        sql = f"INSERT OR IGNORE INTO {T_HOT_STOCKS} (date, symbol, reason_tags) VALUES (?,?,?)"
        for i in range(0, len(hot_records), 10000):
            conn.executemany(sql, hot_records[i:i+10000])
        conn.commit()

        logger.info(f"  强势股: {len(hot)} 行, {hot['symbol'].nunique()} 只")
        return len(hot)
    finally:
        conn.close()


def _load_industry_cache() -> pd.DataFrame:
    """加载行业成分股缓存 [symbol, industry, industry_l1]。

    优先 SW2（申万二级，~130行业），不存在则降级到旧 EM 缓存（~26行业）。
    2026-06-29: 保留 industry_l1 列，用于 SW1 级聚合。
    """
    from pathlib import Path
    cache_dir = Path(__file__).resolve().parents[2] / "data/raw/_cache"

    # 优先 SW2 缓存
    sw2_caches = sorted(cache_dir.glob("sw2_industry_cons_*.parquet"))
    if sw2_caches:
        df = pd.read_parquet(sw2_caches[0])
        if "symbol" in df.columns and "industry_l2" in df.columns:
            # 统一 industry_l2 列名为 industry，保留 industry_l1
            df = df.rename(columns={"industry_l2": "industry"})
            logger.info(f"SW2 行业缓存: {len(df)} 条, {df['industry'].nunique()} 行业, industry_l1 已保留")
            return df

    # 降级到旧 EM 缓存
    old_caches = sorted(cache_dir.glob("sw_industry_cons_*.parquet"))
    if not old_caches:
        logger.warning("行业成分股缓存不存在，跳过")
        return pd.DataFrame()
    df = pd.read_parquet(old_caches[0])
    if "symbol" not in df.columns or "industry" not in df.columns:
        logger.warning(f"行业成分股缓存列异常: {df.columns.tolist()}")
        return pd.DataFrame()
    logger.info(f"EM 行业缓存(降级): {len(df)} 条, {df['industry'].nunique()} 行业")
    return df


def compute_industry_compare_from_bars(store: SignalStore | None = None,
                                        start: str = "2019-01-01",
                                        end: str | None = None) -> int:
    """从 daily_bars + 行业成分股缓存计算行业日表现。

    替代 download_industry_compare()，不依赖外部 API。
    增删改: 先删待更新日期范围，再批量写入。
    """
    from datetime import datetime
    import pandas as pd
    if store is None:
        store = SignalStore()
    end = end or datetime.now().strftime("%Y-%m-%d")

    industry_df = _load_industry_cache()
    if industry_df.empty:
        logger.warning("行业成分股缓存为空，无法计算行业对比")
        return 0

    sym_to_ind = dict(zip(industry_df["symbol"], industry_df["industry"]))
    industries = sorted(industry_df["industry"].unique())
    stock_counts = industry_df["industry"].value_counts().to_dict()
    logger.info(f"行业成分股: {len(industries)} 个行业, {len(industry_df)} 只")

    # 回看加载（多取 40 天供 MA20 计算，写入时只写 start~end）
    lookback = (pd.to_datetime(start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    conn = store._connect()
    try:
        bars = pd.read_sql(
            "SELECT symbol, date, close, volume FROM daily_bars "
            "WHERE date >= ? AND date <= ? ORDER BY symbol, date",
            conn, params=(lookback, end),
        )
        logger.info(f"  加载 daily_bars: {len(bars)} 行, {bars['symbol'].nunique()} 只 (回看 {lookback})")
    finally:
        conn.close()

    if bars.empty:
        return 0

    # 过滤有行业映射的股票
    bars["industry"] = bars["symbol"].map(sym_to_ind)
    bars = bars.dropna(subset=["industry"])

    # 行业级别量能汇总 + MA20 → volume_ratio
    vol_by_industry = bars.groupby(["date", "industry"], as_index=False)["volume"].sum()
    vol_by_industry = vol_by_industry.sort_values(["industry", "date"])
    vol_by_industry["volume_ma20"] = vol_by_industry.groupby("industry")["volume"].transform(
        lambda x: x.rolling(20, min_periods=5).mean())
    vol_by_industry["volume_ratio"] = vol_by_industry["volume"] / vol_by_industry["volume_ma20"].replace(0, float("nan"))
    vol_by_industry["volume_ratio"] = vol_by_industry["volume_ratio"].fillna(1.0).clip(0.1, 5.0).round(2)
    vol_map = vol_by_industry.set_index(["date", "industry"])["volume_ratio"].to_dict()

    # 计算个股日涨跌幅
    bars = bars.sort_values(["symbol", "date"])
    bars["daily_change"] = bars.groupby("symbol")["close"].transform(
        lambda x: x.pct_change() * 100)
    bars = bars.dropna(subset=["daily_change"])

    # 按行业+日期聚合（涨跌幅均值 + 上涨占比）
    bars["is_up"] = bars["daily_change"] > 0
    grouped = bars.groupby(["date", "industry"], as_index=False).agg(
        daily_change=("daily_change", "mean"),
        up_ratio=("is_up", "mean"),
    )
    grouped["breadth"] = (grouped["up_ratio"] * 100).round(1)
    grouped["volume_ratio"] = grouped.set_index(["date", "industry"]).index.map(
        lambda x: vol_map.get(x, 1.0))
    grouped["stock_count"] = grouped["industry"].map(stock_counts).fillna(0).astype(int)

    # 找到每个行业 leader
    leader_idx = bars.loc[bars.groupby(["date", "industry"])["daily_change"].idxmax()]
    leader_map = leader_idx.set_index(["date", "industry"])[["symbol", "daily_change"]]

    grouped["leader_symbol"] = grouped.set_index(["date", "industry"]).index.map(
        lambda x: leader_map.loc[x, "symbol"] if x in leader_map.index else "")
    grouped["leader_change"] = grouped.set_index(["date", "industry"]).index.map(
        lambda x: leader_map.loc[x, "daily_change"] if x in leader_map.index else 0.0)

    grouped["rank"] = grouped.groupby("date")["daily_change"].rank(ascending=False).astype(int)
    grouped["daily_change"] = grouped["daily_change"].round(2)

    # 只输出 start~end 范围的数据（MA20 用了回看数据）
    write_mask = grouped["date"] >= start
    out = grouped[write_mask].copy()
    out["volume_change"] = 0.0  # 占位，兼容旧表列

    # ── SW2 列名统一（industry → industry_name）──
    out = out[["date", "industry", "daily_change", "volume_change",
               "volume_ratio", "breadth",
               "leader_symbol", "leader_change", "rank", "stock_count"]]
    out = out.rename(columns={"industry": "industry_name"})

    # ═══ SW2 写入 industry_compare_sw2 ═══
    store.store_industry_compare_sw2(out)
    n_sw2 = len(out)

    # ================================================================
    # SW1 级计算: 从 daily_bars 按 industry_l1 聚合
    # ================================================================
    if "industry_l1" in industry_df.columns:
        sym_to_l1 = dict(zip(industry_df["symbol"], industry_df["industry_l1"]))
        l1_col = "industry_l1"
        bars[l1_col] = bars["symbol"].map(sym_to_l1)
        l1_bars = bars.dropna(subset=[l1_col])

        if not l1_bars.empty:
            # 量能
            l1_vol = l1_bars.groupby(["date", l1_col], as_index=False)["volume"].sum()
            l1_vol = l1_vol.sort_values([l1_col, "date"])
            l1_vol["volume_ma20"] = l1_vol.groupby(l1_col)["volume"].transform(
                lambda x: x.rolling(20, min_periods=5).mean())
            l1_vol["volume_ratio"] = l1_vol["volume"] / l1_vol["volume_ma20"].replace(0, float("nan"))
            l1_vol["volume_ratio"] = l1_vol["volume_ratio"].fillna(1.0).clip(0.1, 5.0).round(2)
            l1_vol_map = l1_vol.set_index(["date", l1_col])["volume_ratio"].to_dict()
            l1_stock_counts = industry_df.groupby("industry_l1")["symbol"].nunique().to_dict()

            # 聚合
            l1_grouped = l1_bars.groupby(["date", l1_col], as_index=False).agg(
                daily_change=("daily_change", "mean"),
                up_ratio=("is_up", "mean"),
            )
            l1_grouped["breadth"] = (l1_grouped["up_ratio"] * 100).round(1)
            l1_grouped["volume_ratio"] = l1_grouped.set_index(["date", l1_col]).index.map(
                lambda x: l1_vol_map.get(x, 1.0))
            l1_grouped["stock_count"] = l1_grouped[l1_col].map(l1_stock_counts).fillna(0).astype(int)

            # leader
            l1_leader_idx = l1_bars.loc[l1_bars.groupby(["date", l1_col])["daily_change"].idxmax()]
            l1_leader_map = l1_leader_idx.set_index(["date", l1_col])[["symbol", "daily_change"]]
            l1_grouped["leader_symbol"] = l1_grouped.set_index(["date", l1_col]).index.map(
                lambda x: l1_leader_map.loc[x, "symbol"] if x in l1_leader_map.index else "")
            l1_grouped["leader_change"] = l1_grouped.set_index(["date", l1_col]).index.map(
                lambda x: l1_leader_map.loc[x, "daily_change"] if x in l1_leader_map.index else 0.0)

            l1_grouped["rank"] = l1_grouped.groupby("date")["daily_change"].rank(ascending=False).astype(int)
            l1_grouped["daily_change"] = l1_grouped["daily_change"].round(2)

            # 别名翻译: parquet industry_l1 旧名 → SW_INDEX_MAP 新名
            L1_ALIAS = {"化工":"基础化工","电气设备":"电力设备",
                        "纺织服装":"纺织服饰","商业贸易":"商贸零售","休闲服务":"社服"}
            l1_grouped[l1_col] = l1_grouped[l1_col].map(lambda x: L1_ALIAS.get(x, x))

            # 只输出 start~end 范围
            l1_mask = l1_grouped["date"] >= start
            l1_out = l1_grouped[l1_mask].copy()
            l1_out["volume_change"] = 0.0
            l1_out["level"] = "SW1"
            l1_out = l1_out.rename(columns={l1_col: "industry_name"})
            l1_out = l1_out[["date", "industry_name", "daily_change", "volume_change",
                             "volume_ratio", "breadth",
                             "leader_symbol", "leader_change", "rank", "stock_count", "level"]]
        else:
            l1_out = pd.DataFrame()
    else:
        l1_out = pd.DataFrame()

    # ═══ SW1 写入 industry_compare_sw1 ═══
    n_l1 = 0
    if not l1_out.empty:
        l1_write = l1_out[["date", "industry_name", "daily_change", "volume_change",
                           "volume_ratio", "breadth",
                           "leader_symbol", "leader_change", "rank", "stock_count"]]
        store.store_industry_compare_sw1(l1_write)
        n_l1 = len(l1_write)
        logger.info(f"  SW1 {l1_write['date'].nunique()} 天 x {l1_write['industry_name'].nunique()} 行业 = {n_l1} 行")

    total = n_sw2 + n_l1
    logger.info(f"行业对比计算完成: SW2={n_sw2} + SW1={n_l1} = {total} 行")
    return total
