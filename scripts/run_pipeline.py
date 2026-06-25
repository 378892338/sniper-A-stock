"""全链路日报自动化总管 — python -m scripts.run_pipeline

每天 16:00 自动执行（由 Windows 计划任务触发）：
  ① 校验数据新鲜度（轮询等待，直到更新到今天）
  ② 刷新信号表 → ③ 预计算 L2 因子
  → ④ run_live → ⑤ 生成日报 → ⑥ 推送到 Obsidian

开机补跑：启动时检查最近一次运行日期，缺失的天数逐天补跑。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt
import json
import os
import time as _time

from core.logger import get_logger

from sniper.signals.store import SignalStore
from sniper.signals.download_northbound import download_northbound
from sniper.signals.download_dragon_tiger import download_dragon_tiger
from sniper.signals.download_fund_flow import download_fund_flow
from sniper.signals.download_fund_flow_10jqka import download_fund_flow_10jqka
from sniper.signals.compute_from_bars import compute_hot_stocks_from_bars, compute_industry_compare_from_bars
from sniper.signals.schema import T_INDUSTRY_COMPARE, T_HOT_STOCKS, T_NORTHBOUND, T_DRAGON_TIGER, T_FUND_FLOW

logger = get_logger("scripts.run_pipeline")

# ── 配置 ──

OBSIDIAN_DIR = Path("D:/Obsidian/SecondBrain/000-Projects/05-量化系统")
LAST_RUN_FILE = Path("outputs/reports/last_run.json")


def _load_last_run(mode: str = "full") -> str | None:
    """读取最近一次成功运行日期。

    Args:
        mode: "full" 全量管道 / "intraday" 盘中初稿
    """
    if LAST_RUN_FILE.exists():
        try:
            data = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
            # 兼容旧格式 {"last_date": "2026-06-24"}
            if "full" in data or "intraday" in data:
                return data.get(mode)
            # 旧格式：不分模式，直接取 last_date
            return data.get("last_date")
        except Exception:
            return None
    return None


def _load_last_run_safe(mode: str = "full") -> str | None:
    """读取 last_run，损坏或缺失时回退到仓库实际数据最大日期。"""
    last = _load_last_run(mode)
    if last is not None:
        return last
    # 回退：从 daily_bars 实际数据取最大日期
    try:
        from data.local.warehouse import LocalDataWarehouse
        wh = LocalDataWarehouse()
        conn = wh._connect()
        try:
            cur = conn.execute("SELECT MAX(date) FROM daily_bars")
            row = cur.fetchone()
            if row and row[0]:
                logger.info(f"last_run.json 缺失，从仓库回退: {row[0]}")
                return row[0]
        finally:
            conn.close()
    except Exception:
        pass
    return None


def _save_last_run(date: str, mode: str = "full"):
    """记录最近一次成功运行日期。

    Args:
        date: 日期 YYYY-MM-DD
        mode: "full" 全量管道 / "intraday" 盘中初稿
    """
    LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 保留已有数据，只更新当前 mode
    data = {}
    if LAST_RUN_FILE.exists():
        try:
            data = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    data[mode] = date
    LAST_RUN_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_missing_dates(last_date: str, today: str) -> list[str]:
    """获取 last_date ~ today 之间缺失的交易日。"""
    import pandas as pd
    from sniper.data_router import DataRouter

    router = DataRouter()
    df = router.get_trading_dates(start=last_date, end=today)
    if df.empty:
        return []
    all_dates = sorted(df["date"].tolist())

    # 去掉已跑过的日期（last_date 本身已跑过，从下一个开始）
    missing = [d for d in all_dates if d > last_date]
    return missing


def _get_missing_range(store, table: str, date_col: str, target: str) -> tuple[str, str] | None:
    """查表最新日期，返回缺失范围 (start, target)。
    表为空时从 2019-01-01 开始补。已最新则返回 None。
    """
    conn = store._connect()
    try:
        cur = conn.execute(f"SELECT MAX({date_col}) FROM {table}")
        last = cur.fetchone()[0]
    finally:
        conn.close()
    if last and last >= target:
        return None
    start = last or "2019-01-01"
    return (start, target)


def _verify_daily_bars_coverage(wh, today: str) -> bool:
    """验证 daily_bars 表实际数据覆盖是否达标。

    防御 mark_updated() 在数据加载失败后也写时间戳的场景。
    元数据说"已更新"但实际数据可能不全，必须查表本身。
    """
    try:
        conn = wh._connect()
        try:
            cur = conn.execute("SELECT MAX(date) FROM daily_bars")
            row = cur.fetchone()
            if not row or not row[0]:
                logger.warning("[①] daily_bars 表无数据")
                return False
            latest_date = row[0]
            if latest_date < today:
                logger.warning(f"[①] daily_bars 最新日期 {latest_date} < 目标 {today}")
                return False
            logger.info(f"[①] daily_bars 覆盖验证通过: 最新 {latest_date}")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[①] daily_bars 覆盖验证异常（跳过验证）: {e}")
        return True  # 异常时不阻断，让管道继续


def _verify_index_coverage(wh, today: str) -> bool:
    """验证 index_daily 表实际数据日期。

    同样防御 mark_updated 写今天时间但数据未更新的场景。
    """
    try:
        conn = wh._connect()
        try:
            cur = conn.execute(
                "SELECT MAX(date) FROM index_daily"
            )
            row = cur.fetchone()
            if not row or not row[0]:
                logger.warning("[①] index_daily 表无数据")
                return False
            latest_date = row[0]
            if latest_date < today:
                logger.warning(
                    f"[①] index_daily 最新日期 {latest_date} < 目标 {today}"
                )
                return False
            logger.info(
                f"[①] index_daily 数据覆盖验证通过: {latest_date}"
            )
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[①] index_daily 覆盖验证异常（跳过验证）: {e}")
        return True  # 异常时不阻断


def _get_active_stock_count() -> int:
    """获取活跃股票总数（用于覆盖率计算）。"""
    try:
        from data.local.warehouse import LocalDataWarehouse
        wh = LocalDataWarehouse()
        df = wh.get_stock_list(status="active")
        return len(df) if df is not None and not df.empty else 0
    except Exception:
        return 0


def _sync_with_intelligence(
    today: str,
    coverage_state: 'CoverageState',
    skip_tracker: 'StockSkipTracker',
) -> str:
    """智能数据同步 — 带覆盖率检测 + 个股跳过 + 源质量评分。

    替代原 _wait_for_data() 的死循环逻辑。

    Returns:
        "ok" — 数据已就绪，管道可以继续
        "syncing" — 继续下一轮同步
        "degraded" — 覆盖率已达最佳，接受降级继续
        "failed" — 所有源不可用
    """
    from data.local.warehouse import LocalDataWarehouse

    wh = LocalDataWarehouse()

    # 1. 计算当前覆盖率
    try:
        total_active = _get_active_stock_count()
        import pandas as _pd
        conn = wh._connect()
        try:
            covered_df = _pd.read_sql(
                "SELECT COUNT(DISTINCT symbol) as cnt FROM daily_bars WHERE date = ?",
                conn, params=(today,)
            )
            covered = int(covered_df["cnt"].iloc[0]) if not covered_df.empty else 0
        finally:
            conn.close()
        coverage = covered / total_active if total_active > 0 else 0
    except Exception:
        coverage = 0.0
        covered = 0
        total_active = 0

    # 2. 更新状态机
    state = coverage_state.update(coverage)
    logger.info(f"[①] 覆盖率 {coverage:.1%} ({covered}/{total_active})"
                f" | 状态 {state} | 轮次 {coverage_state.rounds}")

    if state in ("ok", "degraded", "failed"):
        return state

    # 3. 数据源健康探测（只在首轮做）
    if coverage_state.rounds == 1:
        try:
            from data.local.updater import _probe_source_health
            _probe_source_health("600436", "2020-01-01", today)
        except Exception:
            pass

    # 4. 刷新轻量表（每轮都做，快）
    try:
        from data.local.updater import (
            update_stock_list, update_trade_calendar,
            update_market_indices, update_sw_indices,
        )
        update_stock_list(wh)
        update_trade_calendar(wh)
        update_market_indices(wh, end=today)
        update_sw_indices(wh, end=today)
    except Exception as e:
        logger.warning(f"[①] 轻量表更新异常（继续）: {e}")

    # 5. 个股日线 — 带跳过列表
    try:
        stock_df = wh.get_stock_list(status="active")
        all_stocks = stock_df["symbol"].tolist() if stock_df is not None and not stock_df.empty else []
        all_stocks = [s for s in all_stocks if not s.startswith("920")]

        need_update = []
        for sym in all_stocks:
            last = wh.get_last_date("daily_bars", "symbol", sym)
            if last and last >= today:
                continue
            need_update.append(sym)

        # 跳过已标记为不可获取的股票
        before_skip = len(need_update)
        need_update = [s for s in need_update if not skip_tracker.should_skip(s)]
        skipped = before_skip - len(need_update)
        if skipped:
            logger.info(f"[①] 跳过 {skipped} 只不可获取股票（累计 {skip_tracker.get_skip_count()} 只）")

        if need_update:
            from data.local.updater import _fetch_failed_with_fetcher
            success_count, per_stock_results = _fetch_failed_with_fetcher(
                need_update, "2000-01-01", today, wh, skip_validation=True,
            )
            # 记录个股级成败用于跳过列表
            skip_tracker.record_batch(per_stock_results)
            wh.mark_updated("daily_bars", success_count)
            logger.info(f"[①] 本轮获取 {success_count}/{len(need_update)} 只")

    except Exception as e:
        logger.warning(f"[①] 个股日线更新异常（继续下一轮）: {e}")

    return "syncing"


def refresh_signal_tables(date: str) -> None:
    """增量刷新信号表到指定日期（只补缺失）。

    资金流向表使用多源自动降级链：
      同花顺（一次全市场，主源）→ 东方财富（逐股，备源）→ 跳过（兜底）
    """
    from datetime import datetime
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    store = SignalStore()

    # ── 自动构建 SW2 行业缓存（如果不存在或过期）──
    # fetch_sw2_members() 内部有缓存检查，空缓存或多行业缺失时自动补全。
    # 重试策略：失败后等 30s 再试一次（legulegu 反爬限流通常持续 10-30s）。
    logger.info(f"  检查 SW2 行业缓存状态...")
    sw2 = None
    try:
        from data.industry import fetch_sw2_members
        sw2 = fetch_sw2_members()
    except Exception as e:
        logger.warning(f"  SW2 首次抓取失败，30s 后重试: {e}")
        _time.sleep(30)
        try:
            from data.industry import fetch_sw2_members
            sw2 = fetch_sw2_members()
        except Exception as e2:
            logger.warning(f"  SW2 第二次抓取失败: {e2}")
            sw2 = None

    if sw2 is not None and not sw2.empty:
        logger.info(f"  SW2 缓存就绪: {sw2['industry_l2'].nunique()} 行业, {len(sw2)} 只")
    else:
        logger.warning("  SW2 缓存暂不可用（API 超载），将降级到 EM 缓存")

    for table, date_col, func, kwargs in [
        (T_INDUSTRY_COMPARE, "date", compute_industry_compare_from_bars, {}),
        (T_HOT_STOCKS,      "date", compute_hot_stocks_from_bars,      {}),
        (T_DRAGON_TIGER,    "date", download_dragon_tiger,             {}),
        (T_NORTHBOUND,      "date", download_northbound,               {}),
    ]:
        r = _get_missing_range(store, table, date_col, date)
        if r is None:
            logger.info(f"  {table} 已最新，跳过")
            continue
        start, target = r
        logger.info(f"  {table} 缺失 {start}~{target}，开始更新...")
        func(store, start=start, end=target, **kwargs)

    # 资金流向多源自动降级
    r = _get_missing_range(store, T_FUND_FLOW, "date", date)
    if r is not None:
        start, target = r
        logger.info(f"  {T_FUND_FLOW} 缺失 {start}~{target}，开始多源更新...")
        fund_success = False
        for source_name, source_func in [
            ("同花顺", download_fund_flow_10jqka),
            ("东方财富", download_fund_flow),
        ]:
            try:
                source_func(store, start=start, end=target)
                fund_success = True
                logger.info(f"  资金流向: {source_name} 源成功")
                break
            except Exception as e:
                logger.warning(f"  资金流向: {source_name} 源失败 ({e})，尝试下一个...")
                continue
        if not fund_success:
            logger.warning(f"  资金流向: 所有数据源均失败，使用现有数据（可能滞后）")


def _sync_to_server(date: str, html: str) -> None:
    """将日报同步到 quant-server 的 report_cache"""
    import json

    SYNC_URL = "http://localhost:8765/api/report/sync"
    SYNC_API_KEY = "pipeline-sync-key-2026"

    try:
        payload = json.dumps({"date": date, "full_html": html}).encode("utf-8")
        import urllib.request

        req = urllib.request.Request(
            SYNC_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {SYNC_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            logger.info(f"  日报同步到 quant-server 成功: {result.get('detail', '')}")
    except Exception as e:
        logger.warning(f"  日报同步到 quant-server 失败（不影响 Obsidian 写入）: {e}")


def _run_single_day(date: str) -> bool:
    """执行一天的完整链路。返回是否成功。"""
    today = dt.date.today().strftime("%Y-%m-%d")

    logger.info(f"{'='*50}")
    logger.info(f"全链路运行: {date}")
    logger.info(f"{'='*50}")

    # 并发守卫：全量管道检测盘中锁
    _intraday_lock = Path("outputs/.intraday.lock")
    if _intraday_lock.exists():
        logger.warning("[①] 盘中管道仍在运行，等待至多 5 分钟...")
        for _ in range(30):
            if not _intraday_lock.exists():
                break
            _time.sleep(10)
        if _intraday_lock.exists():
            logger.warning("[①] 等待超时，盘中锁仍存在，强制继续全量管道")

    # ① 数据新鲜度校验 — 智能同步（覆盖率状态机 + 个股跳过）
    if date == today:
        from shared.source_reliability import CoverageState, StockSkipTracker
        coverage_state = CoverageState()
        skip_tracker = StockSkipTracker()

        while True:
            status = _sync_with_intelligence(today, coverage_state, skip_tracker)
            if status == "ok":
                logger.info(f"[①] 数据同步完成（覆盖率 {coverage_state.best_coverage:.1%}）")
                break
            if status == "degraded":
                logger.warning(
                    f"[①] 数据同步降级（覆盖率 {coverage_state.best_coverage:.1%}），"
                    f"使用现有数据生成日报"
                )
                break
            if status == "failed":
                logger.warning(
                    "[①] 所有数据源不可用，继续使用现有数据生成日报"
                )
                break
            # status == "syncing" → 继续下一轮

    # ①.5 更新估值数据（腾讯实时行情，try/except 不卡流程）
    logger.info(f"[①.⑤/⑦] 更新估值数据...")
    try:
        from scripts.update_valuation import update_valuation
        ok = update_valuation()
        if ok:
            logger.info("估值数据更新成功")
        else:
            logger.warning("估值数据更新被跳过（校验未通过或数据为空）")
    except Exception as e:
        logger.warning(f"估值数据更新异常（继续后续步骤）: {e}")

    # ② 刷新信号表（5张表增量补缺，全部0滞后）
    logger.info(f"[②/⑦] 刷新信号表...")
    try:
        refresh_signal_tables(date)
    except Exception as e:
        logger.warning(f"信号表刷新部分失败（继续生成日报）: {e}")

    # ③ L2 预计算 — 数据已就绪，阻塞执行，失败则终止当天
    logger.info(f"[③/⑦] 预计算 L2 因子...")
    try:
        from scripts.precompute_l2 import precompute_all
        precompute_all(end=date)
        logger.info(f"[③] L2预计算完成")
    except Exception as e:
        logger.error(f"[③] L2预计算失败: {e}")
        return False

    # ④ 跑 day strategy（L0 → configure → trade → append tape）
    logger.info(f"[④/⑦] 运行策略...")
    try:
        from scripts.run_live import daily_run
        daily_run(date)
    except Exception as e:
        logger.error(f"[④] 策略运行失败: {e}")
        return False

    # ⑤ 生成日报（HTML + Markdown + 更新 Obsidian 索引）
    logger.info(f"[⑤/⑦] 生成日报...")
    from scripts.daily_report_html import generate_html, _generate_md
    try:
        html = generate_html(date)
        if not html:
            logger.warning(f"非交易日 {date}，跳过写入")
            _save_last_run(date)
            return True
    except Exception as e:
        logger.error(f"[⑤] 日报生成失败: {e}")
        return False

    md_content = _generate_md(date)

    # ⑥ 写入 Obsidian
    logger.info(f"[⑥/⑦] 写入 Obsidian...")
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        html_path = OBSIDIAN_DIR / f"量化日报_{date}.html"
        html_path.write_text(html, encoding="utf-8")
        md_path = OBSIDIAN_DIR / f"量化日报-{date}.md"
        md_path.write_text(md_content, encoding="utf-8")
        logger.info(f"日报已写入: {html_path}")

        from scripts.daily_report_html import _update_obsidian_index
        _update_obsidian_index(OBSIDIAN_DIR, date, html_path, md_path)
        logger.info(f"索引已更新")
    except Exception as e:
        logger.warning(f"写入 Obsidian 失败（html 已生成不阻断）: {e}")

    # ⑦ 同步到 quant-server
    _sync_to_server(date, html)

    _save_last_run(date)
    logger.info(f"全链路完成: {date}")
    return True


def run_pipeline(target_date: str | None = None) -> bool:
    """全链路入口。返回是否全部成功。

    Args:
        target_date: 指定日期。为 None 时自动补跑。
    """
    today = dt.date.today().strftime("%Y-%m-%d")

    if target_date:
        return _run_single_day(target_date)

    # ── 开机补跑逻辑 ──
    # 只检查全量管道是否已跑过（不被盘中初稿干扰）
    last_run = _load_last_run(mode="full")
    if last_run is None:
        logger.info("首次运行，从今天开始")
        return _run_single_day(today)

    if last_run >= today:
        logger.info(f"已是最新（full 模式上次运行: {last_run}），跳过")
        return True

    # 从缺失日期开始逐天补跑
    missing = _get_missing_dates(last_run, today)
    if not missing:
        logger.info(f"无可补跑日期（最近: {last_run})")
        if last_run < today:
            return _run_single_day(today)
        return True

    all_ok = True
    logger.info(f"发现 {len(missing)} 个缺失交易日: {missing[0]} ~ {missing[-1]}")
    for d in missing:
        ok = _run_single_day(d)
        if not ok:
            logger.warning(f"补跑失败: {d}，继续下一个")
            all_ok = False
            continue
    return all_ok


# ═══════════════════════════════════════════
# 盘中初稿模式（12:00 → 14:45）
# ═══════════════════════════════════════════

_LOCK_FILE = Path("outputs/.intraday.lock")
_HEARTBEAT_INTERVAL = 60


def _acquire_intraday_lock() -> bool:
    """获取 PID 锁，防止管道重叠。返回 True 表示获取成功。"""
    import os as _os
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _LOCK_FILE.exists():
        try:
            stale_pid = int(_LOCK_FILE.read_text().strip().split()[0])
            # Windows 等效 kill -0
            import subprocess as _sp
            result = _sp.run(
                ["tasklist", "/FI", f"PID eq {stale_pid}"],
                capture_output=True, text=True,
            )
            if str(stale_pid) in result.stdout:
                logger.info(f"[intraday] 前序管道 PID={stale_pid} 仍在运行，跳过")
                return False
            # PID 不在运行 → 锁已僵死
            logger.info(f"[intraday] 清理僵死锁 (PID={stale_pid})")
        except Exception:
            pass
    _LOCK_FILE.write_text(f"{_os.getpid()} {_time.time()}")
    return True


def _update_lock_heartbeat():
    """更新锁文件 mtime 作为心跳。"""
    import os as _os
    try:
        _os.utime(_LOCK_FILE, None)
    except Exception:
        pass


def _release_intraday_lock():
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _is_trading_day(date_str: str) -> bool:
    """检查指定日期是否为交易日。"""
    try:
        from sniper.data_router import DataRouter
        router = DataRouter()
        df = router.get_trading_dates(start=date_str, end=date_str)
        return not (df is None or df.empty)
    except Exception:
        return True  # 无法判断时放行


def _calculate_dynamic_cutoff(coverage: float, remaining: int) -> tuple:
    """根据当前进度计算动态截止时间。

    Returns:
        (syncs_cutoff_time, data_end_time)
    """
    import datetime as _dt
    if coverage >= 0.95 or remaining < 50:
        return (_dt.time(14, 15), _dt.time(14, 30))  # 数据好，充裕窗口
    elif coverage >= 0.70:
        return (_dt.time(14, 10), _dt.time(14, 20))  # 中等，压缩窗口
    else:
        return (_dt.time(14, 5), _dt.time(14, 10))   # 差，强截止


def _sync_until_deadline(today: str) -> tuple[float, int]:
    """12:00-14:30 动态截止的增量数据同步。

    Returns:
        (coverage, remaining_stocks) — 供上游判断是否跳过 L2
    """
    from data.local.warehouse import LocalDataWarehouse
    from data.local.updater import _probe_source_health, _check_daily_bars_holes
    import datetime as _dt

    SYNCS_CUTOFF = _dt.time(14, 15)
    DATA_END     = _dt.time(14, 30)

    logger.info(f"[intraday] 增量同步开始: 默认截止 {SYNCS_CUTOFF}, 终裁 {DATA_END}")

    # 交易日守卫
    if not _is_trading_day(today):
        logger.info(f"[intraday] 非交易日 {today}，跳过盘中同步")
        return (0.0, 0)

    # 健康探测仅一次
    _probe_source_health("600436", "2020-01-01", today)

    wh = LocalDataWarehouse()
    from data.local.updater import (
        update_stock_list, update_trade_calendar,
        update_market_indices, update_sw_indices,
        update_daily_bars_all,
    )

    # 检测全局空洞
    try:
        holes = _check_daily_bars_holes(wh, "2026-06-01", today)
        if holes:
            logger.warning(f"[intraday] 发现 {len(holes)} 天全局空洞，将在增量中补缺")
    except Exception:
        pass

    total_active = 0
    try:
        df = wh.get_stock_list(status="active")
        total_active = len(df) if df is not None and not df.empty else 0
    except Exception:
        pass

    while True:
        now = _dt.datetime.now().time()

        # 动态截止：根据当前覆盖率和剩余股数计算
        try:
            import pandas as _pd
            conn = wh._connect()
            try:
                covered_df = _pd.read_sql(
                    "SELECT COUNT(DISTINCT symbol) as cnt FROM daily_bars WHERE date = ?",
                    conn, params=(today,)
                )
                covered = int(covered_df["cnt"].iloc[0]) if not covered_df.empty else 0
            finally:
                conn.close()
        except Exception:
            covered = 0
        coverage = covered / total_active if total_active > 0 else 0
        remaining = total_active - covered

        sync_cutoff, data_end = _calculate_dynamic_cutoff(coverage, remaining)
        logger.info(f"[intraday] 覆盖 {coverage:.1%} ({covered}/{total_active})"
                    f" | 同步截止 {sync_cutoff} | 终裁 {data_end}")

        if now >= data_end:
            logger.info(f"[intraday] 到达终裁时间 {data_end}")
            break

        # 轻量表每轮更新
        try:
            update_stock_list(wh)
            update_trade_calendar(wh)
            update_market_indices(wh, end=today)
            update_sw_indices(wh, end=today)
        except Exception as e:
            logger.warning(f"[intraday] 轻量表更新异常: {e}")

        # 日线拉取截止守卫
        if now >= sync_cutoff:
            logger.info(f"[intraday] 已过同步截止 {sync_cutoff}，停止日线拉取")
            break

        # 增量日线（只补缺失）
        try:
            update_daily_bars_all(wh, end=today, skip_validation=True, incremental=True)
        except Exception as e:
            logger.warning(f"[intraday] daily_bars 增量更新异常: {e}")

        # 达标检查
        try:
            if _verify_daily_bars_coverage(wh, today):
                logger.info(f"[intraday] 数据覆盖达标 ({coverage:.1%})，提前完成")
                break
        except Exception:
            pass

        _update_lock_heartbeat()
        _time.sleep(120)  # 增量模式很快，120s 轮询

    logger.info(f"[intraday] 数据同步结束: 覆盖 {coverage:.1%}, 剩余 {remaining}")
    return (coverage, remaining)


def _run_intraday(date: str) -> bool:
    """盘中初稿模式：增量同步 → 信号 → L2（可跳过）→ 初稿 → Obsidian（14:45 终裁）。"""
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")

    if date != today:
        logger.info(f"[intraday] 非今日 {date}，降级为全量模式")
        return _run_single_day(date)

    # 交易日守卫
    if not _is_trading_day(today):
        logger.info(f"[intraday] 非交易日 {today}，跳过")
        return True

    # 并发守卫：超过 14:45 不再等个股日线，直接出初稿
    now = _dt.datetime.now()
    if now.hour >= 14 and now.minute >= 45:
        logger.warning("[intraday] 超过 14:45，降级为快速模式（跳过个股日线+L2，直接用现有数据出初稿）")
        return _run_intraday_light(today)

    if not _acquire_intraday_lock():
        logger.warning("[intraday] 无法获取锁，退出")
        return False

    try:
        logger.info(f"{'='*50}")
        logger.info(f"[intraday] 盘中管道: {date}")
        logger.info(f"{'='*50}")

        # ① 增量数据同步（12:00-14:30 动态截止）
        coverage, remaining = _sync_until_deadline(date)

        # ② 信号刷新
        logger.info("[intraday] 刷新信号表...")
        try:
            refresh_signal_tables(date)
        except Exception as e:
            logger.warning(f"[intraday] 信号刷新异常（继续）: {e}")

        # ③ L2 预计算（覆盖率不足时跳过）
        l2_ok = True
        if coverage < 0.70 and remaining > 100:
            logger.warning(f"[intraday] 覆盖率 {coverage:.1%} < 70%，跳过 L2 预计算")
            l2_ok = False
        else:
            logger.info("[intraday] L2 预计算...")
            try:
                from scripts.precompute_l2 import precompute_all
                precompute_all(end=date)
            except Exception as e:
                logger.error(f"[intraday] L2 预计算失败: {e}")
                l2_ok = False

        # ④ 生成盘中初稿
        logger.info("[intraday] 生成盘中初稿...")
        from scripts.daily_report_html import generate_intraday_html, _generate_md
        try:
            html = generate_intraday_html(date, skip_l2=not l2_ok)
            if not html:
                logger.warning(f"[intraday] 非交易日 {date}，跳过")
                _save_last_run(date, mode="intraday")
                return True
        except Exception as e:
            logger.error(f"[intraday] 初稿生成失败: {e}")
            return False

        md_content = _generate_md(date)

        # ⑤ 写入 Obsidian（14:45 终裁）
        OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)
        html_path = OBSIDIAN_DIR / f"量化日报_{date}_盘中初稿.html"
        md_path = OBSIDIAN_DIR / f"量化日报-{date}_盘中初稿.md"
        html_path.write_text(html, encoding="utf-8")
        md_path.write_text(md_content, encoding="utf-8")
        logger.info(f"[intraday] 初稿已写入: {html_path}")

        from scripts.daily_report_html import _update_obsidian_index
        _update_obsidian_index(OBSIDIAN_DIR, date, html_path, md_path)

        _sync_to_server(date, html)
        _save_last_run(date, mode="intraday")
        logger.info(f"[intraday] 盘中管道完成: {date}")
        return True

    finally:
        _release_intraday_lock()


def _run_intraday_light(date: str) -> bool:
    """快速盘中模式 — 不拉个股日线，仅指数+信号+初稿。"""
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")
    logger.info(f"[intraday-light] 快速模式: {date}")

    if not _acquire_intraday_lock():
        return False
    try:
        try:
            from data.local.warehouse import LocalDataWarehouse
            from data.local.updater import update_market_indices, update_sw_indices
            wh = LocalDataWarehouse()
            update_market_indices(wh, end=today)
            update_sw_indices(wh, end=today)
        except Exception as e:
            logger.warning(f"[intraday-light] 指数更新异常: {e}")

        try:
            refresh_signal_tables(date)
        except Exception:
            pass

        from scripts.daily_report_html import generate_intraday_html, _generate_md
        html = generate_intraday_html(date, skip_l2=True)
        if html:
            OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)
            html_path = OBSIDIAN_DIR / f"量化日报_{date}_盘中初稿.html"
            html_path.write_text(html, encoding="utf-8")
            md_content = _generate_md(date)
            md_path = OBSIDIAN_DIR / f"量化日报-{date}_盘中初稿.md"
            md_path.write_text(md_content, encoding="utf-8")
            logger.info(f"[intraday-light] 初稿已写入: {html_path}")
            _save_last_run(date, mode="intraday")
        return True
    finally:
        _release_intraday_lock()


def main():
    import argparse
    a = argparse.ArgumentParser(description="全链路日报自动化")
    a.add_argument("--date", type=str, default="", help="指定日期 YYYY-MM-DD，默认自动补跑")
    a.add_argument("--mode", type=str, default="full",
                   choices=["full", "intraday"],
                   help="full=完整管道 intraday=盘中初稿(跳过策略)")
    args = a.parse_args()
    if args.mode == "intraday":
        ok = _run_intraday(args.date or dt.date.today().strftime("%Y-%m-%d"))
    else:
        ok = run_pipeline(args.date or None)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
