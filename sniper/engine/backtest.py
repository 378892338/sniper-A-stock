"""两阶段回测引擎 — 逐日模拟 + 参数自进化"""

import datetime
import pandas as pd
import numpy as np

import sniper.config as CFG
from sniper.data_router import DataRouter
from sniper.layers.l0_market import MarketScorer
from sniper.layers.l1_sector import SectorScorer, FusionOrchestrator
from sniper.layers.l2_stock import StockScorer
from sniper.layers.l3_entry import EntryFilter
from sniper.layers.l4_exit import ExitChain
from sniper.engine.risk import RiskManager
from core.logger import get_logger

try:
    from sniper.engine.backtest_cache import BacktestCache, _cache_key
except ImportError:
    BacktestCache = None
    _cache_key = None

logger = get_logger("sniper.engine.backtest")


class BacktestEngine:
    """逐日模拟回测引擎。

    流程: 每日循环 → L0 择时 → L1 板块 → L2 选股 → L3 入场 → L4 退出 → 风控。

    参数自进化 (self_evolve=True):
      - 每笔平仓后自动: 写 TradeLog → 更新 OnlineLearner → 追踪滚动 Sharpe
      - 绩效衰减 > 50% → 自动回滚参数
      - 在线学习置信度 → 自动调整仓位
      - 月度到期 → 自动触发全量校准

    用法:
      # 回测验证（模拟自进化过程）
      engine.run("2019-01-01", "2026-05-08", self_evolve=True)

      # 实盘（当天交易 + 自动学习）
      engine.run("2026-06-03", "2026-06-03", self_evolve=True)

      # 快速参数搜索（无自进化）
      engine.run("2019-01-01", "2026-05-08")
    """

    def __init__(self, router: DataRouter | None = None,
                 param_manager=None, learner=None):
        self.router = router or DataRouter()
        self.market = MarketScorer(self.router)
        self.sector = SectorScorer(self.router)
        self.fusion = FusionOrchestrator(self.router)  # ETF融合编排器(新增)
        self.stock_scorer = StockScorer(self.router)
        self.entry = EntryFilter(self.router)
        self.exit_chain = ExitChain(self.router)
        self.risk = RiskManager(CFG.BACKTEST.initial_capital)

        self.daily_logs: list[dict] = []
        # 评分缓存
        self._l0_cache: dict[str, float] = {}
        self._l1_cache: dict[str, list[str]] = {}
        self._l1_df_cache: dict[str, pd.DataFrame] = {}  # 板块全量评分缓存（分层 Top N 用）
        # 日线内存缓存 {symbol: DataFrame}
        self._bars_cache: dict[str, pd.DataFrame] = {}

        # 预计算标志
        self._etf_precomputed: bool = False

        # ── 动态配置回调（策略对比用）──
        self._daily_config_callback = None

        # ── 参数自进化（惰性初始化）──
        self._pm = param_manager
        self._learner = learner
        self._trade_log_store = None
        self._entry_scores: dict[str, dict] = {}  # symbol → {l0, l1_rank, l2}
        self._last_5_pnls: list[float] = []
        self._peak_rolling_sharpe = 0.0
        self._last_month = ""
        self._eval = None

    def _precompute_l0_l1(self, dates: list[str]) -> None:
        """预计算 L0/L1 评分（回测前一次性调用）。

        存储每日期 L0 合成评分 + 各子维度评分（打字机用）。
        全量 DataFrame 缓存用于分层 Top N 选取。
        """
        logger.info(f"预计算 L0/L1 评分: {len(dates)} 天")
        for date in dates:
            if date not in self._l0_cache:
                self._l0_cache[date] = self.market.score_all(date)
            if date not in self._l1_cache:
                self._l1_cache[date] = self.sector.top_sectors(date)
            if date not in self._l1_df_cache:
                sw2_names = self.sector.top_sw2_sectors(date, single_layer=False)
                if sw2_names:
                    self._l1_df_cache[date] = pd.DataFrame({"industry_name": sw2_names})
                else:
                    self._l1_df_cache[date] = pd.DataFrame()
        logger.info(f"L0/L1 预计算完成: L0={len(self._l0_cache)} 天, L1={len(self._l1_cache)} 天")

    def _preload_bars_cache(self, symbols: list[str]):
        """一次性预加载所有候选个股的全部日线到内存。"""
        if not symbols:
            return
        conn = self.router.wh._connect()
        try:
            placeholders = ",".join("?" for _ in symbols)
            df = pd.read_sql(
                f"SELECT * FROM daily_bars WHERE symbol IN ({placeholders}) ORDER BY symbol, date",
                conn, params=symbols,
            )
        finally:
            conn.close()

        for sym, grp in df.groupby("symbol"):
            grp = grp.copy()
            grp["date"] = pd.to_datetime(grp["date"])
            self._bars_cache[sym] = grp.set_index("date").sort_index()

        logger.info(f"[缓存] 加载 {len(self._bars_cache)} 只股票日线")

    def _get_cached_bars(self, symbol: str, date: str) -> pd.DataFrame:
        """从内存缓存获取个股日线。"""
        bars = self._bars_cache.get(symbol)
        if bars is None:
            bars = self.router.get_daily_bars(symbol, start=date, end=date)
            return bars
        if date in bars.index:
            row = bars.loc[[date]]
            row = row.reset_index()
            row["date"] = row["date"].astype(str)
            return row
        return pd.DataFrame()

    def _get_entry_price(self, symbol: str, date: str) -> float | None:
        """获取入场价（当日开盘价）。"""
        bars = self._get_cached_bars(symbol, date)
        if bars.empty:
            return None
        return float(bars.iloc[0].get("open", 0))

    def _get_exit_price(self, symbol: str, date: str) -> float | None:
        """获取出场价（当日收盘价）。"""
        bars = self._get_cached_bars(symbol, date)
        if bars.empty:
            return None
        return float(bars.iloc[0].get("close", 0))

    def _ensure_self_evolve(self):
        """惰性初始化自进化组件。"""
        if self._pm is not None:
            return  # 已初始化

        try:
            from sniper.engine.param_manager import ParamManager
            from sniper.engine.online_learner import OnlineParamLearner
            from sniper.engine.trade_log import TradeLogStore
            from sniper.engine.evaluator import PerformanceEvaluator
            self._pm = ParamManager(active=True)
            self._learner = OnlineParamLearner()
            self._trade_log_store = TradeLogStore()
            self._eval = PerformanceEvaluator(CFG.BACKTEST.initial_capital)
            logger.info("[自进化] 组件就绪")
        except Exception as e:
            logger.debug(f"[自进化] 初始化跳过: {e}")
            self._pm = None

    def _post_trade_hook(self, trade: dict):
        """每笔平仓后自动执行（零开销，入参是 risk.trades 里的 SELL 记录）。

        这个 hook 不依赖任何外部数据，不影响主循环性能。
        """
        if self._pm is None:
            return

        pnl_pct = trade.get("pnl_pct", 0) or 0

        # 1. 写 TradeLog
        if self._trade_log_store is not None:
            try:
                import json
                from sniper.engine.trade_log import TradeLog
                entry_date_str = trade.get("entry_date", "")
                # 用 entry_date 查当时的 L0/L1/L2 评分（存到 _entry_scores 中）
                score_key = trade.get("symbol", "")
                entry_scores = getattr(self, "_entry_scores", {}).get(score_key, {})
                log = TradeLog(
                    symbol=trade.get("symbol", ""),
                    sector=trade.get("sector", ""),
                    entry_date=entry_date_str,
                    exit_date=trade.get("date", ""),
                    entry_price=trade.get("entry_price", 0),
                    exit_price=trade.get("price", 0),
                    shares=trade.get("shares", 0),
                    hold_days=trade.get("hold_days", 0),
                    pnl=trade.get("pnl", 0),
                    pnl_pct=pnl_pct,
                    exit_reason=trade.get("reason", ""),
                    l0_score=entry_scores.get("l0", 50.0),
                    l1_sector_rank=entry_scores.get("l1_rank", 1),
                    l2_score=entry_scores.get("l2", 0.0),
                    params_snapshot=self._pm.get_json(),
                )
                self._trade_log_store.append(log)
            except Exception as e:
                logger.debug(f"[自进化] TradeLog 写入失败: {e}")

        # 2. 更新在线学习
        if self._learner is not None:
            try:
                current_params = self._pm.get()
                self._learner.observe(current_params, pnl_pct)
            except Exception as e:
                logger.debug(f"[自进化] 在线学习失败: {e}")

        # 3. 滚动绩效追踪（最近5笔平仓）
        self._last_5_pnls.append(pnl_pct)
        if len(self._last_5_pnls) > 5:
            self._last_5_pnls.pop(0)

        # 4. 自动回滚检测
        if len(self._last_5_pnls) == 5:
            recent_sharpe = self._calc_rolling_sharpe(self._last_5_pnls)
            if self._peak_rolling_sharpe > 0 and recent_sharpe < self._peak_rolling_sharpe * 0.5:
                logger.warning(
                    f"[自进化] 5笔Sharpe={recent_sharpe:.2f} 峰值={self._peak_rolling_sharpe:.2f}"
                    f" 衰减{(1-recent_sharpe/self._peak_rolling_sharpe):.0%}，自动回滚"
                )
                self._pm.rollback(1)
                self._last_5_pnls.clear()
                self._peak_rolling_sharpe = 0
            else:
                self._peak_rolling_sharpe = max(self._peak_rolling_sharpe, recent_sharpe)

    @staticmethod
    def _calc_rolling_sharpe(pnls: list[float]) -> float:
        """计算最近 N 笔平仓的 Sharpe。"""
        if len(pnls) < 5:
            return 0.0
        arr = np.array(pnls)
        mean = np.mean(arr)
        std = np.std(arr, ddof=1)
        if std < 1e-10:
            return 0.0
        # 年化近似: 假设 5 笔 ≈ 25 个交易日
        return mean / std * np.sqrt(252 / 25)

    def _check_monthly_calibration(self, current_date: str):
        """月末自动触发校准。"""
        if self._pm is None or self._eval is None:
            return
        month = current_date[:7]
        if month == self._last_month:
            return
        self._last_month = month

        # 每月最后一天触发
        try:
            dt = datetime.datetime.strptime(current_date, "%Y-%m-%d")
            next_day = dt + datetime.timedelta(days=1)
            if next_day.month != dt.month:  # 当天是月最后一天
                logger.info(f"[自进化] 月频校准触发: {current_date}")
                if self._trade_log_store is not None:
                    trades_df = self._trade_log_store.query(
                        start_date=f"{month}-01",
                        end_date=current_date,
                    )
                    report = self._eval.monthly_calibration(current_date, trades_df)
                    suggestions = report.get("suggestions", {})
                    if suggestions:
                        n = self._pm.apply_batch_suggestions(suggestions)
                        if n > 0:
                            logger.info(f"[自进化] 月频校准: {n} 项参数自动更新")
        except Exception as e:
            logger.warning(f"[自进化] 月频校准失败: {e}")

    def _daily_self_evolve(self, current_date: str):
        """每日自动运行（零开销，不依赖交易事件）。"""
        if self._pm is None:
            return None

        # 动态仓位缩放: 在线学习置信度
        size_scalar = None
        if self._learner is not None:
            try:
                suggestions = self._learner.suggest()
                # 取所有参数的平均置信度
                confs = [s["confidence"] for s in suggestions.values()
                         if isinstance(s, dict) and "confidence" in s]
                if confs:
                    avg_conf = sum(confs) / len(confs)
                    # 置信度 0.5 → 仓位缩放 0.7, 置信度 0.9 → 仓位缩放 1.0
                    size_scalar = 0.5 + avg_conf * 0.5
            except Exception as e:
                logger.debug(f"[自进化] 在线学习置信度失败: {e}")

        # 月度校准检查
        self._check_monthly_calibration(current_date)

        return size_scalar

    def run(self, start_date: str = "", end_date: str = "",
            params: dict | None = None, cache: bool = False,
            self_evolve: bool = False,
            use_precomputed: bool = True,
            l0_cache: dict | None = None,
            l1_cache: dict | None = None,
            l1_full_cache: dict | None = None,
            etf_fusion: bool = True) -> dict:
        """运行回测。

        Args:
            start_date: 开始日期
            end_date: 结束日期
            params: 参数哈希（用于结果缓存）
            cache: 是否启用结果缓存
            self_evolve: 是否启用参数自进化
            l0_cache: 外部预计算的 L0 缓存（多进程优化用）
            l1_cache: 外部预计算的 L1 缓存
            l1_full_cache: 外部预计算的 L1 全量 DataFrame 缓存
            etf_fusion: 是否启用ETF动量融合(默认True, 设False可做AB对比)
        """
        start_date = start_date or CFG.BACKTEST.start_date
        end_date = end_date or CFG.BACKTEST.end_date

        # ── 检查结果缓存 ──
        if cache:
            bc = BacktestCache()
            ck = _cache_key(start_date, end_date, params)
            cached = bc.get(ck)
            if cached is not None:
                logger.info(f"[缓存命中] {ck}")
                return cached

        # 获取交易日历
        cal = self.router.get_trading_dates(start_date, end_date)
        if cal.empty:
            logger.error("无交易日数据")
            return {}
        dates = sorted(cal["date"].tolist())
        logger.info(f"回测区间: {dates[0]} ~ {dates[-1]}, {len(dates)} 交易日")

        # ── 配置ETF融合 ──
        if not etf_fusion:
            self.fusion.pure_sw1_mode = True
            logger.info("[ETF] AB对比模式: pure_sw1_mode=True, 跳过ETF融合")

        # ── 预计算 L0/L1（支持外部缓存注入，多进程优化用）──
        if l0_cache is not None:
            self._l0_cache = l0_cache
            self._l1_cache = l1_cache or {}
            self._l1_df_cache = l1_full_cache or {}
            logger.info(f"[缓存] 使用外部 L0/L1 缓存: L0={len(l0_cache)} 天")
        else:
            self._precompute_l0_l1(dates)

        # ── 预计算 ETF 信号（回测模式, 保证 T-1 时序）──
        if etf_fusion and not self._etf_precomputed:
            try:
                self.fusion.precompute_etf_signal(dates)
                self._etf_precomputed = True
                logger.info(f"[ETF] 信号预计算完成: {len(dates)} 天")
            except Exception as e:
                logger.warning(f"[ETF] 信号预计算失败(降级纯SW1): {e}")

        # ── 预计算 L2 因子缓存（一次性加载全部因子到内存）──
        if use_precomputed:
            try:
                n = self.stock_scorer._load_factor_cache(dates)
                if n:
                    logger.info(f"[预计算] L2 因子 {len(n)} 天就绪")
                else:
                    logger.info(f"[预计算] L2 因子缓存不存在，使用实时计算")
            except Exception as e:
                logger.debug(f"[预计算] L2 因子加载跳过: {e}")

        # ── 初始化参数自进化 ──
        if self_evolve:
            self._ensure_self_evolve()
            monthly_note = f"，月频校准" if self._pm is not None else ""
            logger.info(f"[自进化] 已开启{monthly_note}")

        # 防止同一日重复入场相同股票
        recent_stopped: set[str] = set()
        # 追踪买入价（用于 TradeLog）
        _entry_prices: dict[str, tuple[float, str]] = {}  # symbol → (price, date)

        for i, current_date in enumerate(dates):
            # 前一交易日（用于决策）
            prev_date = dates[i - 1] if i > 0 else current_date

            # ── L0 市场状态 ──
            l0_info = self._l0_cache.get(prev_date, self.market.score_all(prev_date))
            l0_score = l0_info["composite"]

            # ── 自进化（月频校准，不涉及仓位缩放）──
            if self_evolve:
                self._daily_self_evolve(current_date)

            # L0 档位提醒：当 L0 在偏强区间时提示关注仓位（2026-06-06 Strategy A）
            # 警告: 70=满仓档（硬编码），bullish_threshold=64=开仓档，两码事。
            #       L0在[64,70)区间处于偏强但不是极强，提醒关注仓位降至30%。
            if l0_score < 70 and l0_score >= CFG.MARKET.bullish_threshold:
                logger.info(
                    f"[仓位提醒] {current_date} L0={l0_score:.1f} 偏强区间，"
                    f"当前仓位可考虑降至30%"
                )

            # L0 < bullish_threshold → 不开新仓，存量持仓自然退出
            if l0_score < CFG.MARKET.bullish_threshold:
                self.daily_logs.append({
                    "date": current_date, "action": "不开新仓",
                    "l0": round(l0_score, 1),
                })
            else:
                self.daily_logs.append({
                    "date": current_date, "action": "可开仓",
                    "l0": round(l0_score, 1),
                })

            # ── L4 退出检查 ──
            for sym in list(self.risk.positions.keys()):
                pos = self.risk.positions[sym]
                exit_signal = self.exit_chain.evaluate(
                    sym, pos["entry_date"], current_date,
                    pos["entry_price"], pos["highest_price"],
                )
                if exit_signal and exit_signal.get("exit"):
                    price = exit_signal.get("exit_price") or self._get_exit_price(sym, current_date) or pos["entry_price"] * 0.95
                    reason = exit_signal["reason"]

                    # L4 退出三层逻辑（2026-06-06 Strategy A，不引用 bullish_threshold）
                    # 注意: 70=满仓档（硬编码），不可用 bullish_threshold=64 替代。
                    #       因为 64~69 是"偏强减到30%"档，不是满仓档。
                    if l0_score >= 70 and reason in ("初始止损", "动态止盈"):
                        # L0≥70 强势市场：减半仓保留（卖50%留50%）
                        self.risk.reduce_position(sym, price, current_date, ratio=0.5, reason=reason)
                    elif l0_score >= CFG.RISK.active_reduction_l0 and reason in ("初始止损", "动态止盈"):
                        # 64≤L0<70 偏强市场：减到30%（卖70%留30%）
                        self.risk.reduce_position(sym, price, current_date, ratio=0.7, reason=reason)
                    else:
                        # L0<64 弱势市场：全平止损
                        self.risk.close_position(sym, price, current_date, reason)
                        recent_stopped.add(sym)

                        # ── 自进化：每笔平仓后自动学习 ──
                        if self_evolve and self.risk.trades:
                            last_trade = self.risk.trades[-1]
                            if last_trade.get("action") == "SELL" and last_trade.get("pnl") is not None:
                                ep = _entry_prices.pop(sym, None)
                                if ep:
                                    last_trade["entry_price"] = ep[0]
                                    last_trade["entry_date"] = ep[1]
                                ed = last_trade.get("entry_date", current_date)
                                try:
                                    ed_dt = datetime.datetime.strptime(ed, "%Y-%m-%d")
                                    cd_dt = datetime.datetime.strptime(current_date, "%Y-%m-%d")
                                    last_trade["hold_days"] = max(1, (cd_dt - ed_dt).days)
                                except ValueError:
                                    last_trade["hold_days"] = 1
                                last_trade["pnl_pct"] = last_trade.get("pnl", 0) / max(last_trade.get("cost", 1), 1)
                                self._post_trade_hook(last_trade)

            # 清理停损记录
            if len(recent_stopped) > 50:
                recent_stopped.clear()

            # ── 风控检查 ──
            if self.risk.check_max_loss():
                logger.warning(f"{current_date} 触发最大回撤限制，停止交易")
                break

            # ── 组合回撤风控：总净值从峰值回撤5%强制平仓，平仓后继续运行 ──
            if self.risk.is_portfolio_drawdown_triggered():
                dd_pct = self.risk.portfolio_drawdown_pct * 100
                logger.warning(
                    f"{current_date} 组合回撤 {dd_pct:.2f}% 触发预警线 "
                    f"(限制 {CFG.RISK.portfolio_drawdown_limit*100:.0f}%)，强制平仓"
                )
                for sym in list(self.risk.positions.keys()):
                    price = self._get_exit_price(sym, current_date) or 0
                    if price > 0:
                        self.risk.close_position(sym, price, current_date, "组合风控强制平仓")
                logger.info(f"{current_date} 组合回撤强制平仓完成，后续可继续开仓")
                # 不下 break，平仓后继续运行，后续 L0 回升可再开新仓

            # ── L0 < 64 主动减仓到总仓位的30% ──
            if l0_score < CFG.RISK.active_reduction_l0:
                target_exposure = self.risk.total_capital * CFG.RISK.active_reduction_exposure
                current_exposure = self.risk.total_exposure
                if current_exposure > target_exposure and self.risk.positions:
                    reduce_ratio = 1.0 - (target_exposure / current_exposure)
                    if reduce_ratio > 0.01:
                        logger.info(
                            f"[主动减仓] {current_date} L0={l0_score:.1f}<{CFG.RISK.active_reduction_l0}, "
                            f"暴露{current_exposure:.0f}→目标{target_exposure:.0f}, "
                            f"减仓{reduce_ratio:.1%}"
                        )
                        for sym in list(self.risk.positions.keys()):
                            pos = self.risk.positions[sym]
                            price = self._get_exit_price(sym, current_date) or pos["entry_price"] * 0.95
                            self.risk.reduce_position(sym, price, current_date, ratio=reduce_ratio, reason="主动减仓(L0<64)")

            # ── L2 + L3 入场（L0 ≥ bullish_threshold=64 时开新仓）──
            if l0_score >= CFG.MARKET.bullish_threshold and self.risk.can_open_new():
                # 板块选择: ETF融合模式 vs 纯SW1模式
                if etf_fusion and not self.fusion.pure_sw1_mode:
                    top_sectors = self.fusion.top_sectors(
                        prev_date, top_n=CFG.SECTOR.top_n_high, l0_score=l0_score)
                else:
                    actual_top_n = CFG.SECTOR.top_n_high
                    df = self._l1_df_cache.get(prev_date)
                    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                        df = self.sector.composite_scores(prev_date)
                    top_sectors = df.head(actual_top_n)["industry_name"].tolist() if not df.empty else []

                if not top_sectors:
                    self.daily_logs.append({"date": current_date, "action": "无板块"})
                    self.risk.daily_report(current_date)
                    continue

                top_stocks = self.stock_scorer.top_stocks(prev_date, top_sectors)
                max_new = CFG.RISK.max_positions - self.risk.position_count

                # 第一遍：收集所有通过过滤的候选
                passing: list[dict] = []
                for candidate in top_stocks:
                    if len(passing) >= max_new:
                        break
                    sym = candidate["symbol"]
                    if sym in self.risk.positions or sym in recent_stopped:
                        continue
                    check_bars = self._get_cached_bars(sym, current_date)
                    if check_bars.empty:
                        continue
                    sector = candidate.get("sector", "")
                    sector_rank = 1
                    if sector and sector in top_sectors:
                        sector_rank = top_sectors.index(sector) + 1
                    result = self.entry.filter(candidate, prev_date, sector_rank)
                    if result.get("entry"):
                        candidate["_sector_rank"] = sector_rank
                        candidate["_entry_result"] = result
                        passing.append(candidate)

                # 按目标暴露动态分配预算（2026-06-06 Strategy A）
                # 🔴 active_reduction_exposure=0.30 双重用途: ①L0<64减仓目标 ②L0[64,70)入场目标
                # 注意: 70=满仓档（硬编码），不可用 bullish_threshold=64 替代。
                #       L0≥70满仓50%暴露，L0[64,70)偏强30%暴露，L0<64不开仓。
                if l0_score >= 70:
                    dynamic_target = CFG.RISK.target_exposure_ratio        # 0.50
                else:
                    dynamic_target = CFG.RISK.active_reduction_exposure     # 0.30
                remaining_budget = max(0.0, self.risk.total_capital * dynamic_target - self.risk.total_exposure)
                budget_per_stock = remaining_budget / max(1, len(passing))

                # 第二遍：开仓
                entered = 0
                for candidate in passing:
                    if not self.risk.can_open_new():
                        break
                    sym = candidate["symbol"]
                    price = self._get_entry_price(sym, current_date)
                    if price and price > 0:
                        self.risk.open_position(
                            sym, price, current_date,
                            sector=top_sectors[0],
                            score=candidate.get("score", 0),
                            allocated_budget=budget_per_stock,
                        )
                        _entry_prices[sym] = (price, current_date)
                        self._entry_scores[sym] = {
                            "l0": l0_score,
                            "l1_rank": candidate["_sector_rank"],
                            "l2": candidate.get("score", 0),
                        }
                        entered += 1
                if entered > 0:
                    logger.info(
                        f"[入场] {current_date}: 新开 {entered} 仓, "
                        f"预算 {budget_per_stock:.0f}/只, "
                        f"暴露 {self.risk.total_exposure:.0f}/{self.risk.total_capital*dynamic_target:.0f}(目标{dynamic_target:.0%})"
                    )

            # ── 日终更新 ──
            all_prices = {}
            for sym in list(self.risk.positions.keys()):
                bars = self._get_cached_bars(sym, current_date)
                if not bars.empty:
                    all_prices[sym] = float(bars.iloc[0].get("close", 0))
                else:
                    bars_all = self._bars_cache.get(sym)
                    if bars_all is not None and not bars_all.empty:
                        all_prices[sym] = float(bars_all.iloc[-1]["close"])
                    else:
                        logger.warning(f"[回测] {sym} 无历史行情数据，跳过")
            self.risk.update_positions(all_prices)
            self.risk.daily_report(current_date)

            if (i + 1) % 200 == 0:
                logger.info(f"回测进度: {i+1}/{len(dates)} 日, NAV={self.risk.total_capital:.0f}")

        logger.info(f"回测完成. 最终资金: {self.risk.total_capital:.2f}")
        result = self._build_result()

        # ── 保存结果缓存 ──
        if cache:
            try:
                bc = BacktestCache()
                bc.save(ck, result, params)
            except Exception as e:
                logger.debug(f"[缓存] 保存失败: {e}")

        if self_evolve and self._pm is not None:
            logger.info(f"[自进化] 回测结束. 最终参数: "
                        f"stop_loss={self._pm.get_value('stop_loss'):.2f}, "
                        f"position_size={self._pm.get_value('position_size'):.2f}, "
                        f"soft_min_score={self._pm.get_value('soft_min_score'):.0f}")

        return result

    def _build_result(self) -> dict:
        """构建回测结果汇总。"""
        return {
            "final_capital": self.risk.total_capital,
            "total_return": (self.risk.total_capital - CFG.BACKTEST.initial_capital) / CFG.BACKTEST.initial_capital,
            "trades": self.risk.trades,
            "daily_values": self.risk.daily_values,
            "logs": self.daily_logs,
            "l0_scores": self._l0_cache,  # 打字机用：每日期 L0 合成分+子维度
        }
