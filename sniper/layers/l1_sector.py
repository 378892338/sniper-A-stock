"""L1 板块评分 — 4 维度板块排名 + Beta 中性化"""

import numpy as np
import pandas as pd

# TODO: migrate to `import sniper.config as _cfg` (latent import-time binding risk)
from sniper.config import SECTOR as CFG
from sniper.data_router import DataRouter
from core.logger import get_logger

logger = get_logger("sniper.layers.l1_sector")


class SectorScorer:
    """板块评分器。每日对申万行业进行 4 维评分，输出 Top N 板块。"""

    def __init__(self, router: DataRouter | None = None):
        self.router = router or DataRouter()
        self._ind_cache: pd.DataFrame | None = None
        self._sw2_hierarchy: dict[str, list[str]] | None = None

    def _load_industry_cache(self) -> pd.DataFrame:
        """加载并缓存 SW1+SW2 全量数据（从独立表合并）。"""
        if self._ind_cache is not None:
            return self._ind_cache
        sw1 = self.router.get_industry_compare_sw1_range("1900-01-01", "2099-12-31")
        sw2 = self.router.get_industry_compare_sw2_range("1900-01-01", "2099-12-31")
        combined = pd.concat([sw1, sw2], ignore_index=True) if not sw1.empty or not sw2.empty else pd.DataFrame()
        self._ind_cache = combined
        logger.info(f"[缓存] 行业数据: {len(combined)} 行, {combined['date'].nunique() if not combined.empty else 0} 天")
        return combined

    def score_momentum(self, date: str) -> dict[str, float]:
        """动量维度：每行业最近 N 日累计涨幅。"""
        df = self._load_industry_cache()
        if df.empty:
            return {}
        sub = df[df["date"] <= date].copy()
        if sub.empty:
            return {}
        # 按行业分组取最近 momentum_window 天 daily_change 之和
        result = sub.groupby("industry_name").apply(
            lambda g: g.tail(CFG.momentum_window)["daily_change"].sum()
        )
        return result.to_dict()

    def score_fund_flow(self, date: str) -> dict[str, float]:
        """资金维度：用量能变化代理资金流向。"""
        df = self._load_industry_cache()
        today = df[df["date"] == date]
        if today.empty:
            return {}

        result = {}
        if "volume_ratio" in today.columns:
            for _, row in today.iterrows():
                vr = row.get("volume_ratio", 1.0) or 1.0
                score = max(0, min(100, (vr - 0.3) / 1.2 * 100))
                result[row["industry_name"]] = round(score, 1)
        else:
            vol_col = next((c for c in ["volume_change", "成交额变化", "vol_change"] if c in today.columns), None)
            if vol_col is None:
                logger.debug("行业对比表无量能列，回退等分")
                return {row["industry_name"]: 50.0 for _, row in today.iterrows()}
            for _, row in today.iterrows():
                vc = row.get(vol_col, 0) or 0
                score = max(0, min(100, 50 + vc * 25))
                result[row["industry_name"]] = round(score, 1)
        return result

    def score_breadth(self, date: str) -> dict[str, float]:
        """广度维度：板块内上涨个股占比。"""
        today = self._load_industry_cache()
        today = today[today["date"] == date]
        if "breadth" in today.columns and not today.empty:
            non_null = today[today["breadth"].notna()]
            if not non_null.empty:
                return dict(zip(non_null["industry_name"], non_null["breadth"].round(1)))

        # 回退：强势股表标签统计
        dt = self.router.get_hot_stocks(date)
        if dt.empty:
            industry_df = today
            if industry_df.empty:
                return {}
            sc_col = None
            for col in ["stock_count", "成分股数", "stock_cnt"]:
                if col in industry_df.columns:
                    sc_col = col
                    break
            if sc_col is not None:
                max_count = industry_df[sc_col].max() or 1
                return {
                    row["industry_name"]: max(0, min(100, row[sc_col] / max_count * 100))
                    for _, row in industry_df.iterrows()
                }
            return {row["industry_name"]: 50.0 for _, row in industry_df.iterrows()}

        # 统计每个板块的强势股数量
        stock_counts: dict[str, int] = {}
        for _, row in dt.iterrows():
            # reason_tags 可能包含行业信息
            tags = str(row.get("reason_tags", ""))
            if tags:
                for tag in tags.split(";"):
                    tag = tag.strip()
                    if tag:
                        if tag not in stock_counts:
                            stock_counts[tag] = 0
                        stock_counts[tag] += 1

        if not stock_counts:
            # 无行业标签，回退到等分
            return {}

        max_count = max(stock_counts.values()) or 1
        return {
            ind: max(0, min(100, count / max_count * 100))
            for ind, count in stock_counts.items()
        }

    def score_heat(self, date: str) -> dict[str, float]:
        """热点维度：题材/强势股覆盖度。"""
        today = self._load_industry_cache()
        today = today[today["date"] == date]
        if today.empty:
            return {}
        max_rank = today["rank"].max() or 1
        return {
            row["industry_name"]: (1 - ((row["rank"] or max_rank) - 1) / max_rank) * 100
            for _, row in today.iterrows()
        }

    def composite_scores(self, date: str,
                         industries: set[str] | None = None) -> pd.DataFrame:
        """合成 4 维评分，返回排名后的 DataFrame。

        Args:
            industries: 可选，只计算这些行业的评分，归一化/Beta中性化均在该集合内。
                        不传则计算全部已加载行业。
        Beta 中性化：将板块评分减去 Beta×大盘收益，
        消除牛市/熊市整体涨跌对板块区分度的影响。
        """
        momentum = self.score_momentum(date)
        fund_flow = self.score_fund_flow(date)
        breadth = self.score_breadth(date)
        heat = self.score_heat(date)

        all_industries = set(momentum) | set(fund_flow) | set(breadth) | set(heat)
        if industries is not None:
            all_industries &= industries
        if not all_industries:
            return pd.DataFrame()

        # 归一化值域：限制到目标集合内（避免 SW2 极端值影响 SW1 归一化）
        m_vals = [momentum.get(ind, 0) for ind in all_industries]
        rows = []
        for ind in all_industries:
            m = self._normalize(momentum.get(ind, 0), m_vals)
            f = fund_flow.get(ind, 50.0)
            b = breadth.get(ind, 50.0)
            h = heat.get(ind, 50.0)
            total = (
                m * CFG.momentum_weight
                + f * CFG.fund_flow_weight
                + b * CFG.breadth_weight
                + h * CFG.heat_weight
            )
            rows.append({
                "industry_name": ind,
                "momentum": round(m, 1),
                "fund_flow": round(f, 1),
                "breadth": round(b, 1),
                "heat": round(h, 1),
                "composite": round(total, 1),
            })

        out = pd.DataFrame(rows)
        if out.empty:
            return out

        # ── Beta 中性化 ──
        out["composite"] = self._beta_neutralize(out["industry_name"], out["composite"])

        out = out.sort_values("composite", ascending=False).reset_index(drop=True)
        out["rank"] = range(1, len(out) + 1)
        return out

    def _load_sw2_hierarchy(self) -> dict[str, list[str]]:
        """加载 SW1→SW2 父子层级 [SW1名: [SW2子行业列表]]（缓存结果避免重复加载）。"""
        if self._sw2_hierarchy is not None:
            return self._sw2_hierarchy
        from pathlib import Path
        import pandas as pd
        cache_dir = Path(__file__).resolve().parents[2] / "data/raw/_cache"
        caches = sorted(cache_dir.glob("sw2_industry_cons_*.parquet"))
        if not caches:
            return {}
        try:
            df = pd.read_parquet(caches[0])
            if "industry_l1" not in df.columns or "industry_l2" not in df.columns:
                return {}
            L1_ALIAS = {"化工":"基础化工","电气设备":"电力设备",
                        "纺织服装":"纺织服饰","商业贸易":"商贸零售","休闲服务":"社服"}
            hierarchy: dict[str, list[str]] = {}
            for _, row in df.iterrows():
                l1 = L1_ALIAS.get(row["industry_l1"], row["industry_l1"])
                l2 = row["industry_l2"]
                if l1 not in hierarchy:
                    hierarchy[l1] = []
                if l2 not in hierarchy[l1]:
                    hierarchy[l1].append(l2)
            self._sw2_hierarchy = hierarchy
            return hierarchy
        except Exception as e:
            logger.debug(f"SW2 层级加载失败: {e}")
            return {}

    def top_sw2_sectors(self, date: str, top_n: int | None = None,
                         single_layer: bool = False) -> list[str]:
        """二级级联筛选: SW1→Top 3→SW2→Top N。

        single_layer=True 时跳过级联，仅 SW1 评分（对比测试用）。
        """
        n = top_n if top_n is not None else CFG.top_n
        if single_layer:
            sw1_names = set(self._load_sw2_hierarchy().keys())
            if not sw1_names:
                df = self.composite_scores(date)
            else:
                df = self.composite_scores(date, industries=sw1_names)
            if df.empty:
                return []
            return df.head(n)["industry_name"].tolist()

        hierarchy = self._load_sw2_hierarchy()
        if not hierarchy:
            df = self.composite_scores(date)
            if df.empty:
                return []
            return df.head(n)["industry_name"].tolist()
        n = top_n if top_n is not None else CFG.top_n

        # Layer 1
        sw1_df = self.composite_scores(date, industries=set(hierarchy.keys()))
        if sw1_df.empty:
            return []
        top_sw1 = sw1_df.head(3)["industry_name"].tolist()

        # Layer 2
        sw2_candidates: set[str] = set()
        for sw1 in top_sw1:
            sw2_candidates.update(hierarchy.get(sw1, []))
        if not sw2_candidates:
            return top_sw1
        sw2_df = self.composite_scores(date, industries=sw2_candidates)
        if sw2_df.empty:
            return top_sw1
        top_sw2 = sw2_df.head(n)["industry_name"].tolist()
        logger.info(f"L1 {date}: SW1 Top3={top_sw1} -> SW2 Top{n}={top_sw2}")
        return top_sw2

    def top_sectors(self, date: str, top_n: int | None = None) -> list[str]:
        """返回 Top N 板块（2026-06-29: 委托给 top_sw2_sectors() 二级级联）。"""
        return self.top_sw2_sectors(date, top_n)

    @staticmethod
    def _normalize(val: float, values: list) -> float:
        if not values:
            return 50.0
        mn, mx = min(values), max(values)
        if mx - mn == 0:
            return 50.0
        return (val - mn) / (mx - mn) * 100

    _benchmark_cache: dict = None
    _beta_cache: dict[str, float] = None

    def _get_benchmark_returns(self) -> pd.Series:
        """获取基准（沪深300）日收益率序列，缓存避免重复查询。"""
        if self._benchmark_cache is None:
            try:
                bm = self.router.get_index_daily("csi300")
                if not bm.empty and "close" in bm.columns:
                    bm = bm.sort_values("date")
                    self._benchmark_cache = bm["close"].pct_change().fillna(0)
                else:
                    self._benchmark_cache = pd.Series(dtype=float)
            except Exception as e:
                logger.debug(f"获取基准收益率失败: {e}")
                self._benchmark_cache = pd.Series(dtype=float)
        return self._benchmark_cache

    def _compute_betas(self, sector_returns: dict[str, pd.Series]) -> dict[str, float]:
        """横截面计算每个板块对沪深300的beta值。"""
        mkt_ret = self._get_benchmark_returns()
        if mkt_ret is None or mkt_ret.empty or len(mkt_ret) < 60:
            return {}
        recent_mkt = mkt_ret.tail(60).values
        betas: dict[str, float] = {}
        for sector_name, sector_series in sector_returns.items():
            if sector_series is None or sector_series.empty:
                betas[sector_name] = 1.0
                continue
            recent_sector = sector_series.tail(60).values
            if len(recent_sector) < 60:
                betas[sector_name] = 1.0
                continue
            cov = np.cov(recent_sector, recent_mkt)[0, 1]
            var = np.var(recent_mkt)
            betas[sector_name] = cov / var if var > 0 else 1.0
        self._beta_cache = betas
        return betas

    def _beta_neutralize(self, industries: pd.Series, scores: pd.Series) -> pd.Series:
        """Beta 中性化：scores -= beta * market_mean。"""
        if industries.empty or len(industries) < 2:
            return scores
        mkt_ret = self._get_benchmark_returns()
        if mkt_ret is None or mkt_ret.empty or len(mkt_ret) < 60:
            return scores
        recent_mkt = mkt_ret.tail(60)
        mkt_mean = recent_mkt.mean()
        if abs(mkt_mean) < 1e-8:
            return scores
        mapped_scores = ((scores - 50) / 50 * 0.1).values
        raw_betas = (scores / 50).clip(0.5, 1.5).values
        neutralized = scores.values - raw_betas * mkt_mean * 100
        return pd.Series(np.clip(neutralized, 0, 100), index=scores.index)


class FusionOrchestrator:
    """ETF融合编排器 — 集成ETF动量层+贝叶斯融合+分歧诊断+降级仲裁。

    保持top_sectors()接口不变(L2/L3/L4零修改)。

    闭环流程:
      1. 冷启动检查 -> 预热/正常
      2. DegradationArbiter仲裁 -> 当日降级级别
      3. 同步屏障(并行ETF评分+SW1评分)
      4. validate_scores()完整性校验
      5. 贝叶斯融合(按降级级别调整)
      6. DDR诊断(纯标记不调权)
      7. 选股输出
    """

    def __init__(self, router: DataRouter | None = None,
                 config_override: dict | None = None):
        from sniper.layers.l1_etf import EtfMomentumScorer
        from sniper.layers.l1_fusion import BayesianPrecisionFusion
        from sniper.layers.l1_divergence import DivergenceDetector
        from sniper.layers.l1_degradation import DegradationArbiter

        self.router = router or DataRouter()
        self.sector_scorer = SectorScorer(self.router)
        self.etf_scorer = EtfMomentumScorer(self.router)
        self.fusion_engine = BayesianPrecisionFusion()
        self.detector = DivergenceDetector()
        self.arbiter = DegradationArbiter()

        # 配置覆盖(支持AB对比测试)
        self._config = config_override or {}

        # 冷启动状态(评审FAIL-18修复)
        self._warmup_state = "COLD"       # COLD -> WARMING -> NORMAL
        self._warmup_days = 0
        self._warmup_required = self._config.get("warmup_days",
            getattr(CFG, 'warmup_days', 5) if hasattr(CFG, 'warmup_days') else 5)
        self._has_sufficient_data_checked = False

        # AB对比开关(评审架构WARN修复)
        self.pure_sw1_mode = self._config.get("pure_sw1_mode", False)

        # ETF缓存
        self._etf_score_cache: dict[str, dict[str, float]] = {}
        self._precomputed = False
        self._executor = None  # 延迟初始化ThreadPoolExecutor

    def _get_executor(self):
        """延迟初始化线程池(评审FAIL-16修复: ThreadPoolExecutor而非asyncio)"""
        if self._executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._executor = ThreadPoolExecutor(max_workers=2)
        return self._executor

    def _has_sufficient_data(self, date: str) -> bool:
        """检查是否有足够历史数据进入预热(终审APPROVED设计)

        条件(全部满足才算sufficient):
          1. >=12/14 ETF各有>=150行数据,且最新日期距date<=15自然日
          2. SW1指数表有实际>=120行数据(从sw_index_daily表查询)
          3. 首次检查后缓存结果
        """
        if self._has_sufficient_data_checked:
            return self._warmup_state != "COLD"

        from data.index_etf import ETF_INDEX_MAP

        # 条件1: ETF覆盖率 >= 12/14, 每只>=150行 + 新鲜度
        etf_ok = 0
        for etf_name in ETF_INDEX_MAP:
            bars = self.router.get_etf_daily(etf_name)
            if bars.empty or len(bars) < 150:
                continue
            if "date" in bars.columns:
                last_date = pd.Timestamp(bars["date"].max())
                days_behind = (pd.Timestamp(date) - last_date).days
                if days_behind <= 15:
                    etf_ok += 1
            else:
                etf_ok += 1
        etf_sufficient = etf_ok >= 12

        # 条件2: SW1有实际数据(从sw_index_daily表查,非交易日历代理)
        try:
            sw1_bars = self.router.wh.get_sw_index_daily("801150",
                start="2000-01-01", end=date)
            sw1_sufficient = not sw1_bars.empty and len(sw1_bars) >= 120
        except Exception:
            sw1_sufficient = False

        self._has_sufficient_data_checked = True
        result = etf_sufficient and sw1_sufficient
        if result:
            logger.info(f"冷启动数据充分: ETF {etf_ok}/{len(ETF_INDEX_MAP)} + SW1充足")
        else:
            logger.info(f"冷启动数据不足: ETF {etf_ok}/{len(ETF_INDEX_MAP)}, "
                        f"SW1={'充足' if sw1_sufficient else '不足'}")
        return result

    def _warmup_check(self, date: str) -> bool:
        """冷启动状态检查, 返回True=可以正常融合, False=走纯SW1"""
        if self._warmup_state == "NORMAL":
            return True

        if self._warmup_state == "COLD":
            if self._has_sufficient_data(date):
                self._warmup_state = "WARMING"
                logger.info("冷启动: COLD -> WARMING (进入5交易日预热期)")
            else:
                logger.info("冷启动: 数据不足,保持COLD(纯SW1)")
                return False

        if self._warmup_state == "WARMING":
            self._warmup_days += 1
            # 预热期间建立滚动基线
            self._collect_warmup_stats(date)
            if self._warmup_days >= self._warmup_required:
                self._warmup_state = "NORMAL"
                logger.info(f"冷启动: WARMING -> NORMAL ({self._warmup_required}日预热完成)")
                return True
            logger.info(f"冷启动: 预热第{self._warmup_days}/{self._warmup_required}日, 纯SW1不下单")
            return False

        return True

    def _collect_warmup_stats(self, date: str):
        """预热期间收集ETF评分稳定性统计"""
        try:
            etf_df = self.etf_scorer.score_all(date)
            if not etf_df.empty:
                pass  # 基线收集逻辑在后续实现
        except Exception:
            pass

    def precompute_etf_signal(self, dates: list[str]):
        """预计算ETF信号(回测前调用)。

        *** 评审FAIL-17修复: 严格执行T-1时序 ***
        T日决策使用T-1的ETF评分(在prev_date数据上计算),不使用>=T日数据
        """
        self._precomputed = True
        for i, date in enumerate(dates):
            if i < 2:  # 前2日数据不足
                self._etf_score_cache[date] = {"mapped": {}, "confidence": {}}
                continue
            prev_date = dates[i - 1]
            try:
                etf_df = self.etf_scorer.score_all(prev_date)
                mapped, conf = self.etf_scorer.map_to_sw1(etf_df)
                self._etf_score_cache[date] = {
                    "mapped": mapped,
                    "confidence": conf,
                }
            except Exception as e:
                logger.warning(f"预计算ETF信号失败 {prev_date}: {e}")
                self._etf_score_cache[date] = {"mapped": {}, "confidence": {}}

        covered = sum(1 for v in self._etf_score_cache.values() if v.get("mapped"))
        logger.info(f"ETF预计算: {covered}/{len(dates)}日有信号")

    def _score_etf_with_timeout(self, date: str) -> tuple[dict, dict]:
        """带超时的ETF评分(评审FAIL-16修复: ThreadPoolExecutor)"""
        etf_df = self.etf_scorer.score_all(date)
        mapped, conf = self.etf_scorer.map_to_sw1(etf_df)
        return mapped, conf

    def _score_sw1_with_timeout(self, date: str) -> pd.DataFrame:
        """带超时的SW1评分"""
        return self.sector_scorer.composite_scores(date)

    def _get_cached_etf_scores(self, date: str) -> dict | None:
        """获取缓存ETF评分(超时兜底)"""
        cache = self._etf_score_cache.get(date)
        if cache and cache.get("mapped"):
            return cache["mapped"]
        return None

    def _get_cached_sw1_scores(self, date: str) -> pd.DataFrame | None:
        """获取缓存SW1评分(超时兜底)"""
        # 回测中已在_precompute中缓存
        return None

    def _fused_top_sectors(self, date: str, top_n: int, l0_score: float) -> list[str]:
        """完整的ETF融合选股流程。"""
        # Step 0: 降级仲裁 + 同步屏障
        executor = self._get_executor()
        etf_mapped = {}
        etf_confidence = {}

        if self._precomputed and date in self._etf_score_cache:
            # 回测模式: 使用预计算(已保证T-1时序)
            cache = self._etf_score_cache[date]
            etf_mapped = cache.get("mapped", {})
            etf_confidence = cache.get("confidence", {})
            sw1_df = self.sector_scorer.composite_scores(date)
        else:
            # 实盘模式: 并行评分带超时(评审FAIL-16修复)
            etf_future = executor.submit(self._score_etf_with_timeout, date)
            sw1_future = executor.submit(self._score_sw1_with_timeout, date)

            from concurrent.futures import TimeoutError
            timeout = getattr(self.arbiter.cfg, 'etf_timeout_seconds', 120)

            try:
                etf_mapped, etf_confidence = etf_future.result(timeout=timeout)
            except TimeoutError:
                cached = self._get_cached_etf_scores(date)
                if cached is not None:
                    etf_mapped = cached
                    self.arbiter.ingest_signal("etf_api", "MAJOR", level_override="YELLOW")
                else:
                    self.arbiter.ingest_signal("etf_api", "CRITICAL", level_override="YELLOW")

            try:
                sw1_df = sw1_future.result(timeout=timeout)
            except TimeoutError:
                cached = self._get_cached_sw1_scores(date)
                if cached is not None:
                    sw1_df = cached
                    self.arbiter.ingest_signal("sw1_api", "MAJOR", level_override="YELLOW")
                else:
                    self.arbiter.ingest_signal("sw1_api", "CRITICAL", level_override="YELLOW")
                    return []  # 无可用数据

        if sw1_df.empty:
            logger.warning(f"{date}: SW1评分无数据,跳过融合")
            return []

        # Step 1: 降级仲裁
        fault_signals = self._detect_faults(etf_mapped)
        level = self.arbiter.resolve(fault_signals)
        w_etf_ratio = self.arbiter.get_smooth_w_etf_ratio()

        # Step 2: 贝叶斯融合(根据降级级别调整)
        if level == "RED":
            logger.warning(f"{date}: RED降级,跳过融合,纯SW1")
            fused_df = sw1_df
        elif level in ("ORANGE", "YELLOW"):
            etf_mapped_scaled = {
                k: v * w_etf_ratio for k, v in etf_mapped.items()
            } if w_etf_ratio > 0 else {}
            etf_conf_scaled = etf_confidence if w_etf_ratio > 0 else {}
            fused_df = self.fusion_engine.fuse(
                sw1_df, etf_mapped_scaled, etf_conf_scaled, l0_score)
        else:
            fused_df = self.fusion_engine.fuse(
                sw1_df, etf_mapped, etf_confidence, l0_score)

        # Step 3: DDR分歧诊断(纯标记不调权)
        diagnoses = self.detector.diagnose(fused_df, etf_mapped)
        divergence_types = {d.industry: d.divergence_type for d in diagnoses}

        # Step 4: 覆盖偏差监控(评审WARN修复)
        if hasattr(self.detector.cfg, 'coverage_gap_enabled') and \
           self.detector.cfg.coverage_gap_enabled:
            gap_report = self.detector.get_coverage_gap_report(fused_df, etf_mapped)
            if gap_report.get("rank_gap") and abs(gap_report["rank_gap"]) > 5:
                logger.info(
                    f"ETF覆盖偏差: rank_gap={gap_report['rank_gap']:.1f}, "
                    f"无ETF行业平均排名={gap_report['uncovered_avg_rank']}"
                )

        # Step 5: 按fused_score取Top N
        sorted_df = fused_df.sort_values("fused_score", ascending=False)
        top = sorted_df.head(top_n)["industry_name"].tolist()
        return top

    def _detect_faults(self, etf_mapped: dict) -> dict[str, str]:
        """检测各模块故障状态,返回{fault_source: level}

        返回值作为DegradationArbiter.resolve()的输入。
        level取值: "RED"/"ORANGE"/"YELLOW"/"GREEN"
        """
        faults: dict[str, str] = {}

        # ETF检测
        if not etf_mapped:
            faults["etf_all_missing"] = "ORANGE"  # 全部缺失
        elif len(etf_mapped) < 10:
            faults["etf_partial"] = "GREEN"       # 部分缺失,不计入
        else:
            faults["etf"] = "GREEN"

        return faults

    def top_sectors(self, date: str, top_n: int | None = None,
                    l0_score: float = 60.0) -> list[str]:
        """主入口 — 保持与SectorScorer.top_sectors()相同接口。

        Args:
            date: 交易日
            top_n: 返回板块数,不传则用默认
            l0_score: L0市场评分(决定ETF融合权重)

        Returns:
            list[str]: Top N板块名列表
        """
        from sniper.config import SECTOR as SECTOR_CFG
        n = top_n if top_n is not None else SECTOR_CFG.top_n

        # AB对比模式: 跳过ETF融合(评审架构WARN修复)
        if self.pure_sw1_mode:
            return self.sector_scorer.top_sectors(date, n)

        # 冷启动检查
        if not self._warmup_check(date):
            return self.sector_scorer.top_sectors(date, n)

        try:
            return self._fused_top_sectors(date, n, l0_score)
        except Exception as e:
            import traceback
            logger.error(
                f"ETF融合异常: {e}\n{traceback.format_exc()}"
                f"回退纯SW1评分"
            )
            return self.sector_scorer.top_sectors(date, n)

    def get_quality_report(self, date: str) -> dict:
        """生成当日的融合质量监控报告"""
        report = {
            "date": date,
            "warmup_state": self._warmup_state,
            "degradation_level": self.arbiter.get_current_level(),
        }
        # 融合质量报告在_fused_top_sectors调用后由fusion_engine生成
        return report

    def _get_benchmark_returns(self) -> pd.Series:
        """获取基准（沪深300）日收益率序列，缓存避免重复查询。"""
        if self._benchmark_cache is None:
            try:
                bm = self.router.get_index_daily("csi300")
                if not bm.empty and "close" in bm.columns:
                    bm = bm.sort_values("date")
                    self._benchmark_cache = bm["close"].pct_change().fillna(0)
                else:
                    self._benchmark_cache = pd.Series(dtype=float)
            except Exception as e:
                logger.debug(f"获取基准收益率失败: {e}")
                self._benchmark_cache = pd.Series(dtype=float)
        return self._benchmark_cache

    def _compute_betas(self, sector_returns: dict[str, pd.Series]) -> dict[str, float]:
        """横截面计算每个板块对沪深300的beta值。

        用最近60个交易日做线性回归: sector_ret = alpha + beta * market_ret + error
        """
        mkt_ret = self._get_benchmark_returns()
        if mkt_ret is None or mkt_ret.empty or len(mkt_ret) < 60:
            return {}

        recent_mkt = mkt_ret.tail(60).values

        betas: dict[str, float] = {}
        for sector_name, sector_series in sector_returns.items():
            if sector_series is None or sector_series.empty:
                betas[sector_name] = 1.0
                continue

            # 对齐日期
            recent_sector = sector_series.tail(60).values
            if len(recent_sector) < 60:
                betas[sector_name] = 1.0
                continue

            # 简单线性回归: beta = cov(s, m) / var(m)
            cov = np.cov(recent_sector, recent_mkt)[0, 1]
            var = np.var(recent_mkt)
            betas[sector_name] = cov / var if var > 0 else 1.0

        self._beta_cache = betas
        return betas

    def _beta_neutralize(self, industries: pd.Series, scores: pd.Series) -> pd.Series:
        """Beta 中性化：scores -= beta * market_mean。

        用横截面回归计算每个板块对沪深300的beta，
        然后减去 beta * 大盘近期均值，消除牛熊整体涨跌影响。
        """
        if industries.empty or len(industries) < 2:
            return scores

        # 用动量得分作为代理收益来计算beta
        mkt_ret = self._get_benchmark_returns()
        if mkt_ret is None or mkt_ret.empty or len(mkt_ret) < 60:
            return scores

        recent_mkt = mkt_ret.tail(60)
        mkt_mean = recent_mkt.mean()

        if abs(mkt_mean) < 1e-8:
            # 大盘均值接近0，无需中性化
            return scores

        # 简化实现：使用板块动量收益做beta
        # 从 composite_scores 中，动量维度已经是0-100的分数
        # 将其映射回收益率空间: (score - 50) / 50 * 0.1 → ±10%收益率
        mapped_scores = ((scores - 50) / 50 * 0.1).values

        # 计算每个板块的beta
        # 这里简化：用动量得分的偏离度作为beta代理
        # 动量得分 > 50 的板块 beta > 1（超大盘股特征）
        # 动量得分 < 50 的板块 beta < 1（防御股特征）
        raw_betas = (scores / 50).clip(0.5, 1.5).values

        # Beta 中性化: adjusted = raw - beta * market_mean
        neutralized = scores.values - raw_betas * mkt_mean * 100

        # 截断到合理范围
        return pd.Series(np.clip(neutralized, 0, 100), index=scores.index)
