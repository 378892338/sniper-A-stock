"""L2 个股评分 — 12 因子综合评分 + 向量化批量计算"""

import pandas as pd
import numpy as np

from sniper.config import STOCK as CFG
from sniper.data_router import DataRouter
from core.logger import get_logger

logger = get_logger("sniper.layers.l2_stock")


class StockScorer:
    """个股评分器。在指定板块内对个股进行 12 因子综合评分。

    性能优化:
      - 批量预加载 OHLCV 数据（1次SQL替代N次）
      - 向量化计算 8 个纯量价因子（numpy/pandas）
      - 资金/龙虎榜因子批量查询（1次SQL替代N次）
    """

    def __init__(self, router: DataRouter | None = None):
        self.router = router or DataRouter()
        self._stock_list: list[str] | None = None
        # 板块→股票映射缓存 (sector_name → list[str])
        self._sector_stock_map: dict[str, list[str]] | None = None

    @property
    def all_stocks(self) -> list[str]:
        if self._stock_list is None:
            conn = self.router.wh._connect()
            try:
                df = pd.read_sql(
                    "SELECT DISTINCT symbol FROM daily_bars ORDER BY symbol",
                    conn,
                )
            finally:
                conn.close()
            self._stock_list = df["symbol"].tolist() if not df.empty else []
        return self._stock_list

    def get_sector_stocks(self, sector: str) -> list[str]:
        """获取板块内个股。基于缓存的行业映射。

        严格过滤：只返回该板块内的已知股票。
        未匹配的板块返回空列表（不回退到全市场），
        保证 L1 的板块过滤能传递到 L2 选股。
        """
        if self._sector_stock_map is None:
            self._sector_stock_map = self._load_sector_map()

        stocks = self._sector_stock_map.get(sector, [])

        # 尝试 EM→SW 名翻译
        if not stocks:
            from data.industry import EM_SW_NAME_MAP
            sw_name = EM_SW_NAME_MAP.get(sector)
            if sw_name and sw_name != sector:
                stocks = self._sector_stock_map.get(sw_name, [])
                if stocks:
                    logger.debug(f"板块 '{sector}' → SW '{sw_name}': {len(stocks)} 只")

        if not stocks:
            logger.debug(f"板块 '{sector}' 无行业映射数据（不在 SW 26 行业中）")

        return stocks

    def _load_sector_map(self) -> dict[str, list[str]]:
        """加载行业映射（从 parquet 缓存 + SW_INDEX_MAP）。

        返回 {sector_name: [symbol1, symbol2, ...]}
        包含同义名映射，便于 L1 的板块名直接查找。
        """
        from pathlib import Path
        from data.industry import SW_INDEX_MAP, EM_SW_NAME_MAP

        cache_dir = Path(__file__).resolve().parents[2] / "data/raw/_cache"
        maps: dict[str, list[str]] = {}
        # 同义名索引: 记录哪些名字是别称，不需要独立数据
        aliases: dict[str, str] = {}

        # ── 从 parquet 缓存读取行业分类（优先 SW2）──
        sw2_caches = sorted(cache_dir.glob("sw2_industry_cons_*.parquet"))
        ind_df = None
        ind_col = None
        if sw2_caches:
            try:
                ind_df = pd.read_parquet(sw2_caches[0])
                ind_col = "industry_l2" if "industry_l2" in ind_df.columns else None
            except Exception as e:
                logger.debug(f"SW2 缓存读取失败: {e}")

        if ind_df is None or ind_df.empty or (ind_col and ind_col not in ind_df.columns):
            old_caches = sorted(cache_dir.glob("sw_industry_cons_*.parquet"))
            if old_caches:
                try:
                    ind_df = pd.read_parquet(old_caches[0])
                    ind_col = "industry" if "industry" in ind_df.columns else None
                    logger.info("行业映射降级: 使用 EM 缓存（SW2 不可用）")
                except Exception as e:
                    logger.debug(f"EM 缓存读取失败: {e}")
                    ind_df = None

        if ind_df is not None and not ind_df.empty and ind_col:
            sym_col = "symbol" if "symbol" in ind_df.columns else None
            if sym_col:
                for _, row in ind_df.iterrows():
                    sym = str(row[sym_col])
                    industry = str(row[ind_col])
                    if industry not in maps:
                        maps[industry] = []
                    if sym not in maps[industry]:
                        maps[industry].append(sym)
                logger.info(f"行业映射: {len(maps)} 个行业, {sum(len(v) for v in maps.values())} 只股票")

        # ── 补全 SW 缺失行业（parquet 不全，用 SW_INDEX_MAP 填充占位）──
        sw_names = set(SW_INDEX_MAP.values())
        existing = set(maps.keys())
        missing = sw_names - existing
        if missing:
            logger.info(f"SW 缺失行业: {missing}")
            # 这些行业在 parquet 中没有数据，股票划到"其他"
            for name in missing:
                maps[name] = []

        # ── 建立同义名索引（EM→SW）──
        for em_name, sw_name in EM_SW_NAME_MAP.items():
            if em_name != sw_name:
                aliases[em_name] = sw_name
                # 如果是 SW 已有别名，复制对应股票列表
                if sw_name in maps and em_name not in maps:
                    maps[em_name] = maps[sw_name][:]

        # ── 加载概念板块映射（补充 SW 未覆盖的板块）──
        con_caches = list(cache_dir.glob("sw_concept_cons_*.parquet"))
        if con_caches:
            try:
                con_df = pd.read_parquet(con_caches[0])
                if not con_df.empty:
                    cols = list(con_df.columns)
                    sym_col = next((c for c in cols if c in ("symbol", "股票代码", "代码")), None)
                    con_col = next((c for c in cols if c in ("concept", "板块名称", "概念")), None)
                    if sym_col and con_col:
                        concept_count = 0
                        for _, row in con_df.iterrows():
                            sym = str(row[sym_col])
                            concept = str(row[con_col])
                            if concept not in maps:
                                maps[concept] = []
                                concept_count += 1
                            if sym not in maps[concept]:
                                maps[concept].append(sym)
                        logger.info(f"概念板块映射: +{concept_count} 个板块")
            except Exception as e:
                logger.debug(f"概念缓存读取失败: {e}")

        # ── 统计 ──
        matched = len(maps)
        total_stocks = sum(len(v) for v in maps.values())
        logger.info(f"行业映射汇总: {matched} 个板块, {total_stocks} 只股票")
        if aliases:
            logger.info(f"  同义名: {aliases}")

        return maps

    # ─────────────────────────────────────────────────────────────
    # 向量化批量评分（核心优化入口）
    # ─────────────────────────────────────────────────────────────

    def top_stocks(self, date: str, sectors: list[str]) -> list[dict]:
        """在指定板块中选 Top N 股票。"""
        candidate_symbols: list[str] = []
        for sector in sectors:
            sector_stocks = self.get_sector_stocks(sector)
            if sector_stocks:
                candidate_symbols.extend(sector_stocks)
        candidate_symbols = list(set(candidate_symbols))
        if not candidate_symbols:
            return []

        factor_df = self._load_precomputed_factors(date, candidate_symbols)
        if factor_df is not None and not factor_df.empty:
            factor_df["score"] = self._compute_composite_score(factor_df)
            factor_df = factor_df.sort_values("score", ascending=False)
            top = factor_df.head(CFG.top_n)
            return [{"symbol": idx, "score": round(row["score"], 1)}
                    for idx, row in top.iterrows()]

        result_df = self._score_batch_vectorized(candidate_symbols, date)
        if result_df.empty:
            return []
        return result_df.head(CFG.top_n).to_dict("records")

    _factor_cache_all: dict[str, pd.DataFrame] | None = None

    def _load_factor_cache(self, dates: list[str] | None = None):
        """一次性加载全部预计算因子到内存。"""
        from pathlib import Path
        cache_dir = Path(__file__).resolve().parents[2] / "outputs/precomputed/l2_factors"
        if not cache_dir.exists():
            return {}

        # 如果已有缓存且传入了 dates，只加载缺失的
        if self._factor_cache_all is None:
            self._factor_cache_all = {}

        # 有指定日期集合时按需加载
        if dates:
            files_to_load = [d for d in dates if d not in self._factor_cache_all]
        else:
            files_to_load = [f.stem for f in sorted(cache_dir.glob("*.parquet"))
                           if f.stem != "_trade_dates" and f.stem not in self._factor_cache_all]

        if not files_to_load:
            return self._factor_cache_all or {}

        loaded = 0
        for date_str in files_to_load:
            fpath = cache_dir / f"{date_str}.parquet"
            if fpath.exists():
                try:
                    self._factor_cache_all[date_str] = pd.read_parquet(fpath)
                    loaded += 1
                except Exception:
                    pass

        logger.info(f"[缓存] 加载 {loaded} 天预计算因子")
        return self._factor_cache_all or {}

    def _load_precomputed_factors(self, date: str,
                                  symbols: list[str]) -> pd.DataFrame | None:
        """从内存缓存或 parquet 获取因子。"""
        # 内存缓存优先
        if self._factor_cache_all is not None and date in self._factor_cache_all:
            df = self._factor_cache_all[date]
            return df[df.index.isin(symbols)] if symbols else df

        # 回退到 parquet 加载
        from pathlib import Path
        cache_dir = Path(__file__).resolve().parents[2] / "outputs/precomputed/l2_factors"
        fpath = cache_dir / f"{date}.parquet"
        if not fpath.exists():
            return None
        try:
            df = pd.read_parquet(fpath)
            return df[df.index.isin(symbols)] if symbols else df
        except Exception:
            return None

    def _load_precomputed_factors(self, date: str,
                                  symbols: list[str]) -> pd.DataFrame | None:
        """从预计算 parquet 缓存加载因子。

        返回: DataFrame(index=symbol, columns=factors), 无缓存时返回 None
        """
        from pathlib import Path

        cache_dir = Path(__file__).resolve().parents[2] / "outputs/precomputed"
        fpath = cache_dir / "l2_factors" / f"{date}.parquet"
        extra_path = cache_dir / "l2_factors_extra" / f"{date}.parquet"

        if not fpath.exists():
            return None

        try:
            df = pd.read_parquet(fpath)
            # 过滤到候选股
            df = df[df.index.isin(symbols)]

            # 加载额外因子（资金/基本面）
            if extra_path.exists():
                try:
                    extra = pd.read_parquet(extra_path)
                    extra = extra[extra.index.isin(symbols)]
                    for col in extra.columns:
                        df[col] = extra[col]
                except Exception:
                    pass

            return df
        except Exception:
            return None

    def _score_batch_vectorized(self, symbols: list[str], date: str) -> pd.DataFrame:
        """向量化批量计算 12 因子评分。

        Returns: DataFrame with columns [symbol, score, trend, volume, macd, rsi, ...]
        """
        # Step 1: 批量加载 OHLCV 数据（1次SQL）
        bars_df = self._preload_bars(symbols)
        if not bars_df:
            return pd.DataFrame()

        # Step 2: 向量化计算 8 个纯量价因子（无SQL查询）
        factors_df = self._compute_technical_factors_batch(bars_df, date)

        # Step 3: 批量查询资金面因子（1次SQL替代N次）
        fund_factors = self._batch_fund_flow_factors(symbols, date)

        # Step 4: 批量查询龙虎榜因子（1次SQL替代N次）
        dragon_tiger_factors = self._batch_dragon_tiger_factors(symbols, date)

        # Step 5: 批量查询基本面因子（1次SQL替代N次）
        fin_factors = self._batch_financial_factors(symbols, date)

        # Step 6: 合并所有因子
        if factors_df is not None:
            for col in fund_factors.columns:
                if col in factors_df.columns:
                    factors_df[col] = factors_df[col].combine_first(fund_factors[col])
                else:
                    factors_df[col] = fund_factors[col]
            for col in dragon_tiger_factors.columns:
                if col in factors_df.columns:
                    factors_df[col] = factors_df[col].combine_first(dragon_tiger_factors[col])
                else:
                    factors_df[col] = dragon_tiger_factors[col]
            for col in fin_factors.columns:
                if col in factors_df.columns:
                    factors_df[col] = factors_df[col].combine_first(fin_factors[col])
                else:
                    factors_df[col] = fin_factors[col]
        else:
            # 无OHLCV数据的股票也加入（使用默认值50）
            factors_df = pd.DataFrame({
                "symbol": symbols,
            })

        # Step 7: 加权合成评分
        factors_df["score"] = self._compute_composite_score(factors_df)

        # Step 8: 过滤无效评分
        factors_df = factors_df[factors_df["score"] > 0].copy()

        return factors_df.sort_values("score", ascending=False)[["symbol", "score"]].head(CFG.top_n)

    def _compute_technical_factors_batch(
        self, bars_df: dict[str, pd.DataFrame], date: str
    ) -> pd.DataFrame:
        """向量化计算 8 个纯量价因子（趋势、量能、MACD、RSI + 市值/换手）。"""
        records = []
        weights = {
            "trend": CFG.trend_factor_weight,
            "volume": CFG.volume_factor_weight,
            "macd": CFG.macd_factor_weight,
            "rsi": CFG.rsi_factor_weight,
            "market_cap": CFG.market_cap_weight,
            "turnover_score": CFG.turnover_weight,
        }

        for sym, bars in bars_df.items():
            recent = bars[bars.index <= date]
            if recent.empty or "close" not in recent.columns:
                continue

            close_series = recent["close"].values.astype(float)
            latest = recent.iloc[-1]

            # 1. 趋势因子
            trend = 50.0
            if len(close_series) >= CFG.momentum_window:
                ma = np.mean(close_series[-CFG.momentum_window:])
                if ma > 0:
                    trend = max(0, min(100, 50 + (latest["close"] - ma) / ma * 200))

            # 2. 量能因子
            volume = 50.0
            if "volume" in recent.columns and len(recent) >= 5:
                vol_ma = recent["volume"].rolling(5).mean().iloc[-1]
                volume = max(0, min(100, (latest.get("volume", 0) / max(vol_ma, 1)) * 50))

            # 3. MACD 因子
            macd = 50.0
            if len(close_series) >= 26:
                ema12 = pd.Series(close_series).ewm(span=12).mean().iloc[-1]
                ema26 = pd.Series(close_series).ewm(span=26).mean().iloc[-1]
                if ema26 != 0:
                    macd = max(0, min(100, 50 + (ema12 - ema26) / ema26 * 500))

            # 4. RSI 因子
            rsi = 50.0
            if len(close_series) >= CFG.rsi_window + 1:
                deltas = np.diff(close_series[-CFG.rsi_window - 1:])
                gains = np.sum(deltas[deltas > 0])
                losses = -np.sum(deltas[deltas < 0])
                if losses == 0:
                    rsi = 100.0
                else:
                    rs = gains / losses
                    rsi = 100 - 100 / (1 + rs)
                rsi = max(0, min(100, rsi))

            # 11. 市值因子
            market_cap = 50.0
            amount = latest.get("amount", 0) or 0
            if amount > 0:
                log_amount = np.log(float(amount))
                market_cap = max(0, min(100, (25 - log_amount) / 10 * 100))

            # 12. 换手率因子
            turnover_score = 50.0
            turnover = latest.get("turnover", 0) or 0
            if turnover > 0:
                turnover_score = max(0, min(100, 100 - turnover * 100))

            records.append({
                "symbol": sym,
                "trend": trend,
                "volume": volume,
                "macd": macd,
                "rsi": rsi,
                "market_cap": market_cap,
                "turnover_score": turnover_score,
            })

        if not records:
            return pd.DataFrame()

        return pd.DataFrame(records)

    def _batch_fund_flow_factors(self, symbols: list[str], date: str) -> pd.DataFrame:
        """批量查询资金流向因子（fund_flow, big_order）。"""
        if not symbols:
            return pd.DataFrame()

        try:
            df = self.router.sig.get_fund_flow_batch(symbols, date)
        except Exception:
            df = pd.DataFrame()

        if df.empty:
            return pd.DataFrame({"symbol": symbols, "fund_flow": float("nan"), "big_order": float("nan")})

        result = {}
        for sym in symbols:
            sym_rows = df[df["symbol"] == sym]
            if sym_rows.empty:
                result[sym] = {"fund_flow": float("nan"), "big_order": float("nan")}
            else:
                main_net = sym_rows[sym_rows["date"] <= date]["main_net"].sum()
                super_large = sym_rows[sym_rows["date"] <= date]["super_large"].sum()
                result[sym] = {
                    "fund_flow": max(0, min(100, 50 + main_net / 1e7 * 10)),
                    "big_order": max(0, min(100, 50 + super_large / 1e7 * 10)),
                }

        return pd.DataFrame([result[sym] for sym in symbols])

    def _batch_dragon_tiger_factors(self, symbols: list[str], date: str) -> pd.DataFrame:
        """批量查询龙虎榜因子（dragon_tiger）。"""
        if not symbols:
            return pd.DataFrame()

        try:
            dt = self.router.sig.get_dragon_tiger(date)
        except Exception:
            dt = pd.DataFrame()

        if dt.empty:
            return pd.DataFrame({"symbol": symbols, "dragon_tiger": float("nan")})

        # 构建 symbol → net_buy 映射
        dt_map = {}
        for _, row in dt.iterrows():
            sym = str(row.get("symbol", ""))
            if sym:
                net_buy = row.get("net_buy", 0) or 0
                dt_map[sym] = max(0, min(100, 50 + net_buy / 1e7 * 20))

        return pd.DataFrame({
            "symbol": symbols,
            "dragon_tiger": [dt_map.get(sym, 50.0) for sym in symbols],
        })

    def _batch_financial_factors(self, symbols: list[str], date: str) -> pd.DataFrame:
        """批量查询基本面因子（eps, roe, revenue_yoy）。"""
        if not symbols:
            return pd.DataFrame()

        try:
            df = self.router.sig.get_latest_quarterly_batch(symbols, date)
        except Exception:
            df = pd.DataFrame()

        if df.empty:
            return pd.DataFrame({
                "symbol": symbols,
                "eps_score": float("nan"),
                "roe_score": float("nan"),
                "revenue_growth": float("nan"),
            })

        result = {}
        for _, row in df.iterrows():
            sym = str(row.get("symbol", ""))
            if not sym:
                continue
            eps = row.get("eps", 0) or 0
            roe = row.get("roe", 0) or 0
            rev_yoy = row.get("revenue_yoy", 0) or 0
            result[sym] = {
                "eps_score": max(0, min(100, 50 + eps * 20)),
                "roe_score": max(0, min(100, roe * 10)),
                "revenue_growth": max(0, min(100, 50 + rev_yoy)),
            }

        out = []
        for sym in symbols:
            if sym in result:
                out.append(result[sym])
            else:
                out.append({"eps_score": float("nan"), "roe_score": float("nan"), "revenue_growth": float("nan")})
        return pd.DataFrame(out)

    def _compute_composite_score(self, df: pd.DataFrame) -> pd.Series:
        """12 因子加权合成评分（NaN-aware + 横截面排名）。

        两步：
          1. 加权合成原始分（NaN-aware，缺失因子跳过）
          2. 横截面百分位排名（0-100），保证评分在全市场范围内有区分度

        横截面排名确保弱市中也能选出相对最强的股票，
        解决"弱市所有因子都弱→评分集中在50附近→无法选股"的问题。
        """
        weights = {
            "trend": CFG.trend_factor_weight,
            "volume": CFG.volume_factor_weight,
            "macd": CFG.macd_factor_weight,
            "rsi": CFG.rsi_factor_weight,
            "fund_flow": CFG.fund_flow_weight,
            "big_order": CFG.big_order_weight,
            "dragon_tiger": CFG.dragon_tiger_weight,
            "eps_score": CFG.eps_weight,
            "roe_score": CFG.roe_weight,
            "revenue_growth": CFG.revenue_growth_weight,
            "market_cap": CFG.market_cap_weight,
            "turnover_score": CFG.turnover_weight,
        }

        cols = [c for c in weights if c in df.columns]
        if not cols:
            return pd.Series(50.0, index=df.index)

        w = np.array([weights[c] for c in cols], dtype=float)
        vals = df[cols].values.astype(float)

        # 逐行计算：跳过 NaN 因子，重归一化权重
        raw_scores = np.full(len(vals), 0.0, dtype=float)
        for i in range(len(vals)):
            row = vals[i]
            mask = ~np.isnan(row)
            if not mask.any():
                continue
            active_w = w[mask]
            active_w = active_w / active_w.sum()
            active_vals = row[mask]
            raw_scores[i] = active_vals @ active_w

        # 横截面百分位排名（关键：保证区分度）
        if len(raw_scores) > 1:
            from scipy.stats import rankdata
            ranks = rankdata(raw_scores, method="average")
            percentile = (ranks - 1) / (len(raw_scores) - 1) * 100
            return pd.Series(percentile, index=df.index)

        return pd.Series(raw_scores * 0 + 50.0, index=df.index)

    # ─────────────────────────────────────────────────────────────
    # 原有单只评分方法（保留向后兼容）
    # ─────────────────────────────────────────────────────────────

    def score_single(self, symbol: str, date: str) -> dict[str, float]:
        """对单只股票计算 12 因子评分，返回因子字典和总分因素。"""
        factors: dict[str, float] = {}
        bars = self.router.get_daily_bars(symbol, end=date)
        if bars.empty:
            return {}

        bars = bars.sort_values("date")
        recent = bars[bars["date"] <= date]

        if recent.empty or "close" not in recent.columns:
            return {}

        close_series = recent["close"].values
        latest = recent.iloc[-1]

        # 1. 趋势因子
        if len(close_series) >= CFG.momentum_window:
            ma = np.mean(close_series[-CFG.momentum_window:])
            factors["trend"] = max(0, min(100, 50 + (latest["close"] - ma) / ma * 200))
        else:
            factors["trend"] = 50.0

        # 2. 量能因子
        if "volume" in recent.columns and len(recent) >= 5:
            vol_ma = recent["volume"].rolling(5).mean().iloc[-1]
            factors["volume"] = max(0, min(100, (latest.get("volume", 0) / max(vol_ma, 1)) * 50))
        else:
            factors["volume"] = 50.0

        # 3. MACD 因子
        if len(close_series) >= 26:
            ema12 = pd.Series(close_series).ewm(span=12).mean().iloc[-1]
            ema26 = pd.Series(close_series).ewm(span=26).mean().iloc[-1]
            macd_val = ema12 - ema26
            factors["macd"] = max(0, min(100, 50 + macd_val / ema26 * 500))
        else:
            factors["macd"] = 50.0

        # 4. RSI 因子
        if len(close_series) >= CFG.rsi_window + 1:
            deltas = np.diff(close_series[-CFG.rsi_window - 1:])
            gains = np.sum(deltas[deltas > 0])
            losses = -np.sum(deltas[deltas < 0])
            if losses == 0:
                rsi = 100.0
            else:
                rs = gains / losses
                rsi = 100 - 100 / (1 + rs)
            factors["rsi"] = max(0, min(100, rsi))
        else:
            factors["rsi"] = 50.0

        # 5-7. 资金/龙虎榜因子（保留原始逻辑）
        ff = self.router.get_fund_flow(symbol)
        if not ff.empty:
            main_net = ff[ff["date"] <= date]["main_net"].sum()
            factors["fund_flow"] = max(0, min(100, 50 + main_net / 1e7 * 10))
            super_large = ff[ff["date"] <= date]["super_large"].sum()
            factors["big_order"] = max(0, min(100, 50 + super_large / 1e7 * 10))
        else:
            factors["fund_flow"] = 50.0
            factors["big_order"] = 50.0

        dt = self.router.get_dragon_tiger(date)
        if not dt.empty:
            row = dt[dt["symbol"] == symbol]
            factors["dragon_tiger"] = max(0, min(100, 50 + row.iloc[0]["net_buy"] / 1e7 * 20)) if not row.empty else 50.0
        else:
            factors["dragon_tiger"] = 50.0

        # 8-10. 基本面因子
        q = self.router.get_quarterly(symbol)
        if not q.empty:
            latest_q = q.iloc[0]
            eps = latest_q.get("eps", 0) or 0
            factors["eps_score"] = max(0, min(100, 50 + eps * 20))
            roe = latest_q.get("roe", 0) or 0
            factors["roe_score"] = max(0, min(100, roe * 10))
            rev_yoy = latest_q.get("revenue_yoy", 0) or 0
            factors["revenue_growth"] = max(0, min(100, 50 + rev_yoy))
        else:
            factors["eps_score"] = 50.0
            factors["roe_score"] = 50.0
            factors["revenue_growth"] = 50.0

        # 11-12. 修正因子
        if "amount" in latest and latest["amount"] > 0:
            factors["market_cap"] = max(0, min(100, (25 - np.log(latest["amount"])) / 10 * 100))
        else:
            factors["market_cap"] = 50.0
        if "turnover" in latest and latest["turnover"] > 0:
            factors["turnover_score"] = max(0, min(100, 100 - latest["turnover"] * 100))
        else:
            factors["turnover_score"] = 50.0

        return factors

    def composite_score(self, symbol: str, date: str) -> float:
        """12 因子加权合成评分 0-100。"""
        factors = self.score_single(symbol, date)
        if not factors:
            return 0.0

        weights = {
            "trend": CFG.trend_factor_weight,
            "volume": CFG.volume_factor_weight,
            "macd": CFG.macd_factor_weight,
            "rsi": CFG.rsi_factor_weight,
            "fund_flow": CFG.fund_flow_weight,
            "big_order": CFG.big_order_weight,
            "dragon_tiger": CFG.dragon_tiger_weight,
            "eps_score": CFG.eps_weight,
            "roe_score": CFG.roe_weight,
            "revenue_growth": CFG.revenue_growth_weight,
            "market_cap": CFG.market_cap_weight,
            "turnover_score": CFG.turnover_weight,
        }

        score = sum(factors.get(k, 50.0) * w for k, w in weights.items())
        total_w = sum(weights.values())
        return score / total_w if total_w > 0 else 50.0

    def _score_from_bars(self, symbol: str, bars: pd.DataFrame, date: str) -> float:
        """用内存中的 bars 计算综合评分（完整 12 因子）。

        兼容旧测试代码：通过向量化路径计算单只股票评分。
        """
        temp_df = self._score_batch_vectorized([symbol], date)
        if temp_df.empty:
            return 0.0
        row = temp_df[temp_df["symbol"] == symbol]
        return float(row["score"].iloc[0]) if not row.empty else 0.0

    def _recent_active_stocks(self, date: str) -> list[str]:
        """SQL 预过滤：近 60 日有成交、价格适中、流动性足够的股票。"""
        from datetime import datetime, timedelta
        dt = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=90)
        start_90d = dt.strftime("%Y-%m-%d")
        conn = self.router.wh._connect()
        try:
            sql = """
                SELECT symbol, MAX(date) as last_date, AVG(close) as avg_price,
                       AVG(volume) as avg_vol, COUNT(*) as days
                FROM daily_bars
                WHERE date >= ? AND date <= ?
                GROUP BY symbol
                HAVING days >= 20 AND avg_price >= 3 AND avg_price <= 300 AND avg_vol >= 500000
            """
            df = pd.read_sql(sql, conn, params=(start_90d, date))
            return df["symbol"].tolist() if not df.empty else []
        finally:
            conn.close()

    def _preload_bars(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """批量预加载个股日线，返回 {symbol: DataFrame} 字典。"""
        if not symbols:
            return {}

        conn = self.router.wh._connect()
        try:
            placeholders = ",".join("?" for _ in symbols)
            sql = f"""
                SELECT * FROM daily_bars
                WHERE symbol IN ({placeholders})
                ORDER BY symbol, date
            """
            df = pd.read_sql(sql, conn, params=symbols)
        finally:
            conn.close()

        result: dict[str, pd.DataFrame] = {}
        if df.empty:
            return result
        for sym, grp in df.groupby("symbol"):
            grp = grp.copy()
            grp["date"] = pd.to_datetime(grp["date"])
            result[sym] = grp.set_index("date").sort_index()
        return result
