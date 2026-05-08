"""调度器 — 周频/日频/即时触发 + 全层集成"""

import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from engine.state_machine import SystemState, StateMachine
from engine.portfolio import Portfolio
from core.logger import get_logger

logger = get_logger("engine.scheduler")


class Scheduler:
    """
    三层漏斗调度器。

    运行节奏:
    - 第一层(周): 每周最后一个交易日正式评估 → 决定仓位目标
    - 第一层(日): 日频预警 → 自动触发降仓/冻结
    - 第二层(周): 与第一层同频，第一层通过后才执行正式评估
    - 第二层(日): 日频预警 → 对已持仓股票所属指数做日线审查
    - 第三层(日): 每天运行，第一第二层都通过 + 上游保鲜验证后才进入
    - 第三层(日): 已推送股票每天追踪退出信号
    """

    def __init__(self, state_machine: StateMachine, total_capital: float | None = None):
        self.sm = state_machine
        self._last_daily_run: str | None = None
        self._last_weekly_run: str | None = None
        self._is_week_end = False
        self._signal_logger = None   # 延迟初始化
        self._reporter = None
        self._portfolio = Portfolio(total_capital) if total_capital else None
        self._downloader = None      # 延迟初始化 DataDownloader
        self._stock_pool_cache: pd.DataFrame | None = None

    @property
    def downloader(self):
        if self._downloader is None:
            from data.downloader import DataDownloader
            self._downloader = DataDownloader()
        return self._downloader

    def _run_pre_filter(self) -> pd.DataFrame:
        """运行前置过滤 + 数据新鲜度检查，返回过滤后的股票池"""
        from gate.pre_filter import run_pre_filter
        from data.downloader import TTL_STOCK_LIST

        if self.downloader.needs_refresh("stock_list", TTL_STOCK_LIST):
            logger.info("前置过滤: 刷新股票池...")
            self._stock_pool_cache = self.downloader.fetch_stock_list()
        elif self._stock_pool_cache is None:
            self._stock_pool_cache = self.downloader.fetch_stock_list()

        return self._stock_pool_cache if self._stock_pool_cache is not None else pd.DataFrame()

    def is_weekly_eval_day(self, dt: datetime = None) -> bool:
        if dt is None:
            dt = datetime.now()
        return dt.weekday() == 4

    def is_trading_day(self, dt: datetime = None) -> bool:
        if dt is None:
            dt = datetime.now()
        return dt.weekday() < 5

    def should_push(self) -> bool:
        return self.is_trading_day()

    @property
    def signal_log(self):
        if self._signal_logger is None:
            from output.signal_log import SignalLogger
            self._signal_logger = SignalLogger()
        return self._signal_logger

    @property
    def reporter(self):
        if self._reporter is None:
            from output.reporter import generate_daily_report, generate_weekly_report
            _Reporter = type("_Reporter", (), {
                "daily": staticmethod(generate_daily_report),
                "weekly": staticmethod(generate_weekly_report),
            })
            self._reporter = _Reporter()
        return self._reporter

    # ── 周度循环 ──

    def weekly_cycle(self, layer1_passed: bool, layer2_passed: bool = False,
                     cross_level_jump: bool = False) -> dict:
        """基础周度循环 — 仅推进状态机，不含数据拉取"""
        self._last_weekly_run = datetime.now().isoformat()
        self._is_week_end = True

        old_state = self.sm.current_state()
        self.sm.weekly_check(layer1_passed, layer2_passed, cross_level_jump)
        new_state = self.sm.current_state()

        if old_state != new_state:
            self.signal_log.log_state_change(old_state.value, new_state.value,
                                             f"L1={layer1_passed} L2={layer2_passed}")

        return {
            "type": "weekly",
            "time": self._last_weekly_run,
            "layer1_passed": layer1_passed,
            "layer2_passed": layer2_passed,
            "old_state": old_state.value,
            "new_state": new_state.value,
        }

    def run_weekly_cycle(self,
                         market_data: dict[str, object] = None,      # {shanghai: df_weekly, ...}
                         market_monthly: dict[str, object] = None,
                         etf_data: dict[str, object] = None,         # {etf_name: df_weekly}
                         etf_monthly: dict[str, object] = None,
                         etf_fund_data: dict[str, dict] = None,
                         csi300_close: object = None,                # 沪深300周线close
                         bypass_cooling: bool = False) -> dict:
        """
        完整周度循环：拉取L1/L2数据 → 评估 → 推送。

        返回: {state, layer1_result, layer2_result, report, actions}
        """
        from gate.layer1_market import assess_market
        from gate.layer2_sector import assess_sectors

        actions = []

        # ── 前置过滤: 刷新股票池 ──
        stock_pool = self._run_pre_filter()
        logger.info(f"股票池: {len(stock_pool)} 只 (经 ST/退市/次新股/流动性过滤)")

        # ── 第一层: 大盘环境 ──
        l1_result = None
        l1_passed = False
        if market_data:
            l1_result = assess_market(market_data, monthly_data=market_monthly)
            l1_passed = l1_result.strong_count >= 2

            # 更新状态机仓位目标
            if l1_passed:
                self.sm.ctx.target_position_pct = l1_result.actual_position_pct

        # ── 第二层: ETF分类指数 ──
        l2_result = None
        l2_passed = False
        if l1_passed and etf_data:
            l1_state_map = {"牛市": "bull", "震荡": "volatile", "偏弱": "weak", "熊市": "bear"}
            l1_market_state = l1_state_map.get(l1_result.market_state, "volatile")
            l2_result = assess_sectors(
                etf_data, etf_monthly,
                benchmark_close=csi300_close,
                fund_data=etf_fund_data,
                l1_market_state=l1_market_state,
            )
            l2_passed = l2_result.passed
            if l2_passed:
                self.sm.ctx.strong_sectors = [s.etf_name for s in l2_result.strong_sectors]

        # ── 推进状态机 ──
        self.weekly_cycle(l1_passed, l2_passed, bypass_cooling)
        new_state = self.sm.current_state()

        # ── 降级过渡 ──
        if old_target := self.sm.ctx._prev_target:
            if l1_passed and l1_result.actual_position_pct < old_target:
                portfolio = self._portfolio
                if portfolio:
                    downgrades = portfolio.calculate_downgrade_actions(
                        l1_result.actual_position_pct
                    )
                    actions.extend(downgrades)
        if l1_passed:
            self.sm.ctx._prev_target = l1_result.actual_position_pct

        # ── 生成周报 ──
        report = self.reporter.weekly(
            layer1_result=l1_result,
            layer2_result=l2_result,
            position_summary={
                "target_pct": self.sm.ctx.target_position_pct,
                "strong_sectors": self.sm.ctx.strong_sectors,
                "state": new_state.value,
            },
        )

        return {
            "state": new_state.value,
            "layer1_result": l1_result,
            "layer2_result": l2_result,
            "report": report,
            "actions": actions,
        }

    # ── 日度循环 ──

    def daily_cycle(self, layer1_alert: dict = None,
                    hold_check: list[dict] = None) -> dict:
        """基础日度循环"""
        today = datetime.now()
        self._last_daily_run = today.isoformat()

        actions = []

        if layer1_alert and layer1_alert.get("triggered"):
            self.sm.on_daily_alert(layer1_alert["level"])
            actions.append({
                "type": "alert",
                "level": layer1_alert["level"],
                "action": "冻结买入",
            })
        elif self.sm.ctx.daily_alert_active and not (layer1_alert and layer1_alert.get("triggered")):
            self.sm.on_daily_alert_cleared()

        if hold_check:
            for check in hold_check:
                if check.get("triggered"):
                    actions.append(check)

        can_push = self.should_push()

        return {
            "type": "daily",
            "time": self._last_daily_run,
            "state": self.sm.current_state().value,
            "actions": actions,
            "can_push": can_push,
        }

    def run_daily_cycle(self,
                        market_daily: dict[str, object] = None,     # {shanghai: df_daily, ...}
                        etf_daily: dict[str, object] = None,        # {etf_name: df_daily}
                        stock_candidates: list[dict] = None,         # [{symbol, daily_df, weekly_df, ...}]
                        holding_positions: dict[str, dict] = None,   # {symbol: {daily_df, weekly_df, sector}}
                        sector_strength: dict[str, bool] = None,
                        etf_l2_scores: dict[str, float] = None,      # {etf_name: L2评分}
                        max_positions: int = 5) -> dict:
        """
        完整日度循环: L1/L2保鲜 → L3评估/选股 → 退出追踪 → 推送。

        返回: {state, freshness, selections, exits, alerts, report}
        """
        from gate.layer1_market import daily_alert_check as l1_daily_alert
        from gate.layer2_sector import check_daily_alert as l2_daily_alert
        from gate.layer3_stock import (
            assess_stock, check_upstream_freshness,
            select_top_stocks_by_etf, SelectionResult,
        )
        from engine.watcher import daily_track_positions

        state = self.sm.current_state()
        actions = []
        alerts = []
        selections = None
        l3_stocks = []

        # ── 前置过滤: 确保股票池新鲜 ──
        self._run_pre_filter()

        # ── L1 日频预警 ──
        l1_alert = None
        if market_daily:
            l1_alert = l1_daily_alert(market_daily)
            if l1_alert and l1_alert.get("triggered"):
                self.sm.on_daily_alert(l1_alert["level"])
                self.signal_log.log_alert(f"L1-{l1_alert['level']}", str(l1_alert.get("signals", [])))
                alerts.append({"layer": "L1", "level": l1_alert["level"],
                               "signals": l1_alert.get("signals", [])})

        # ── L3 上游保鲜 ──
        freshness = None
        if market_daily and self.sm.current_state() in (SystemState.HUNTING, SystemState.HOLDING):
            freshness = check_upstream_freshness(market_daily, etf_daily)

        # ── L3: 个股评估 + 选股 ──
        can_scan = (
            self.sm.can_buy()
            and (freshness is None or freshness.l3_status != "全局暂停")
        )
        if can_scan and stock_candidates and self.sm.current_state() == SystemState.HUNTING:
            for sc in stock_candidates:
                verdict = assess_stock(
                    symbol=sc["symbol"],
                    daily_df=sc["daily_df"],
                    weekly_df=sc.get("weekly_df"),
                    monthly_df=sc.get("monthly_df"),
                    fund_data=sc.get("fund_data"),
                )
                if verdict.passed_gate:
                    l3_stocks.append(verdict)

            if l3_stocks:
                selections = select_top_stocks_by_etf(
                    l3_stocks,
                    etf_l2_scores or {},
                    max_positions=max_positions,
                )
                for sv in selections.selected:
                    self.signal_log.log_buy_signal(
                        sv.symbol, sv.score, sv.chan_buy_point
                    )

        # ── L2 日频预警 ──
        if holding_positions:
            held_etf_map = {}
            for sym, data in holding_positions.items():
                held_etf_map[sym] = data.get("etf_tags", [])
            l2_alert = l2_daily_alert(etf_daily or {}, held_etf_map)
            if l2_alert.get("triggered"):
                self.signal_log.log_alert(f"L2-{l2_alert['level']}", str(l2_alert.get("triggered_etfs", [])))
                alerts.append({"layer": "L2", "level": l2_alert["level"],
                               "etfs": l2_alert.get("triggered_etfs", [])})

        # ── 退出信号追踪 ──
        exits = []
        if holding_positions and self.sm.current_state() in (SystemState.HOLDING, SystemState.HUNTING):
            exits = daily_track_positions(
                holding_positions, sector_strength,
                etf_strong_pool=self.sm.ctx.strong_sectors,
                l1_is_strong=l1_alert is None,
            )
            for ex in exits:
                self.signal_log.log_sell_signal(ex["symbol"], ex.get("reason", ""))
                actions.append(ex)

        # ── 日报 ──
        report = self.reporter.daily(
            layer1_result=None,
            layer2_result=None,
            layer3_stocks=selections.selected if selections else l3_stocks,
            state=self.sm.current_state().value,
            alerts=alerts,
        )

        return {
            "state": state.value,
            "freshness": freshness,
            "selections": selections,
            "exits": exits,
            "alerts": alerts,
            "report": report,
            "actions": actions,
        }

    # ── 推送 ──

    def push_report(self, content: dict) -> dict:
        if not self.should_push():
            return {"pushed": False, "reason": "非交易日不推送"}

        from output.pusher import Pusher
        pusher = Pusher()
        title = content.get("title", "量化系统报告")
        body = self._format_report_body(content)
        pusher.push(title, body)
        return {"pushed": True, "content": content}

    def _format_report_body(self, report: dict) -> str:
        lines = []
        for section in report.get("sections", []):
            lines.append(f"\n{'─'*40}")
            lines.append(f"  {section['header']}")
            lines.append(f"{'─'*40}")
            content = section.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        lines.append(f"  {item.get('symbol', '')}  "
                                     f"评分: {item.get('score', '')}  "
                                     f"买点: {item.get('buy_point', '')}")
                    else:
                        lines.append(f"  {item}")
            elif isinstance(content, dict):
                for k, v in content.items():
                    lines.append(f"  {k}: {v}")
            elif content is not None:
                lines.append(f"  {content}")
        lines.append(f"\n{'─'*40}")
        lines.append(f"  系统状态: {report.get('system_state', '')}")
        lines.append(f"  时间: {report.get('timestamp', '')}")
        return "\n".join(lines)

    # ── 状态报告 ──

    def get_status(self) -> dict:
        """获取当前运行状态"""
        return {
            **self.sm.get_status_report(),
            "last_weekly": self._last_weekly_run,
            "last_daily": self._last_daily_run,
            "signals_today": len(self.signal_log.read_today_signals()),
        }
