"""数据源状态检查器 — 覆盖率、新鲜度、评分模式透明化。

提供对各个数据层独立的状态检查，以及全链路校验。
纯查询，不改数据，不写日志到磁盘。
"""

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from core.logger import get_logger

logger = get_logger("data.freshness")

STALE_WARN_DAYS = 5
STALE_ERROR_DAYS = 15
L2_FACTORS_DIR = Path(__file__).resolve().parents[1] / "outputs/precomputed/l2_factors"


class DataFreshnessChecker:
    """数据源状态检查器。"""

    def __init__(self, router=None):
        if router is None:
            from sniper.data_router import DataRouter
            router = DataRouter()
        self.router = router
        self.wh = router.wh

    # ── 预计算因子状态 ──────────────────────────────

    def precomputed_status(self) -> dict:
        """预计算因子目录状态。"""
        if not L2_FACTORS_DIR.exists():
            return {"available": False, "message": "目录不存在"}

        files = sorted(L2_FACTORS_DIR.glob("2026-*.parquet"))
        files = [f for f in files if f.stem != "_trade_dates"]

        if not files:
            return {"available": False, "message": "无因子文件"}

        latest = files[-1].stem
        latest_df = pd.read_parquet(files[-1])
        total_stocks = len(latest_df)
        total_files = len(files)

        latest_dt = datetime.strptime(latest, "%Y-%m-%d")
        stale_days = (datetime.now() - latest_dt).days

        status = self._stale_status(stale_days)
        return {
            "available": True,
            "latest_date": latest,
            "total_files": total_files,
            "total_stocks": total_stocks,
            "stale_days": stale_days,
            "status": status,
        }

    # ── 信号表新鲜度 ────────────────────────────────

    def signal_freshness(self) -> dict[str, dict]:
        """各信号表的最后更新日期和新鲜度状态。"""
        tables = {
            "industry_compare": "行业对比",
            "hot_stocks": "强势股",
            "fund_flow": "资金流向",
            "dragon_tiger": "龙虎榜",
        }
        result = {}
        conn = self.wh._connect()
        try:
            for tbl, label in tables.items():
                try:
                    df = pd.read_sql(f"SELECT MAX(date) as d FROM {tbl}", conn)
                    last = df.iloc[0, 0]
                    if last is None:
                        result[tbl] = {"label": label, "available": False}
                        continue
                    last_str = str(last)[:10] if hasattr(last, "strftime") else str(last)
                    last_dt = datetime.strptime(last_str, "%Y-%m-%d")
                    stale_days = (datetime.now() - last_dt).days
                    result[tbl] = {
                        "label": label,
                        "available": True,
                        "last_date": last_str,
                        "stale_days": stale_days,
                        "status": self._stale_status(stale_days),
                    }
                except Exception:
                    result[tbl] = {"label": label, "available": False}
        finally:
            conn.close()
        return result

    # ── 日线数据覆盖 ────────────────────────────────

    def daily_bars_coverage(self) -> dict:
        """daily_bars 表中各板块的股票覆盖概况。"""
        conn = self.wh._connect()
        try:
            latest = pd.read_sql("SELECT MAX(date) as d FROM daily_bars", conn)
            latest_date = str(latest.iloc[0, 0])[:10] if latest.iloc[0, 0] is not None else None

            total = pd.read_sql("SELECT COUNT(DISTINCT symbol) as cnt FROM daily_bars", conn).iloc[0, 0]
            cy = pd.read_sql("SELECT COUNT(DISTINCT symbol) as cnt FROM daily_bars WHERE symbol LIKE '30%'", conn).iloc[0, 0]
            sh = pd.read_sql("SELECT COUNT(DISTINCT symbol) as cnt FROM daily_bars WHERE symbol LIKE '60%'", conn).iloc[0, 0]
            sz = pd.read_sql("SELECT COUNT(DISTINCT symbol) as cnt FROM daily_bars WHERE symbol LIKE '00%'", conn).iloc[0, 0]
            kcb = pd.read_sql("SELECT COUNT(DISTINCT symbol) as cnt FROM daily_bars WHERE symbol LIKE '688%'", conn).iloc[0, 0]
            others = total - cy - sh - sz - kcb
        finally:
            conn.close()

        return {
            "latest_date": latest_date,
            "total": total,
            "by_board": {
                "创业板(30)": cy,
                "沪主板(60)": sh,
                "深主板(00)": sz,
                "科创板(688)": kcb,
                "其他": max(0, others),
            },
        }

    # ── 覆盖差距分析 ──────────────────────────────

    def universe_gap(self, as_of_date: str | None = None) -> dict:
        """预计算因子 vs 数据库的股票覆盖率差距。

        按日期对比：只有当天在 daily_bars 中有数据的股票才算"活的"，
        避免计算有历史无近期数据的股票（如新入库但数据不全的股票）。

        Args:
            as_of_date: 目标日期。默认用最新因子文件日期。
        """
        # 取最新因子文件
        if not L2_FACTORS_DIR.exists():
            return {"message": "无因子目录，无法对比"}
        files = sorted(L2_FACTORS_DIR.glob("2026-*.parquet"))
        files = [f for f in files if f.stem != "_trade_dates"]
        if not files:
            return {"message": "无因子文件，无法对比"}

        latest_date = as_of_date or files[-1].stem
        target_file = L2_FACTORS_DIR / f"{latest_date}.parquet"
        if not target_file.exists():
            # fallback 到最新文件
            target_file = files[-1]
            latest_date = target_file.stem

        df_pre = pd.read_parquet(target_file)
        pre_symbols = set(df_pre.index.astype(str))

        conn = self.wh._connect()
        try:
            # 只比较当天有数据的股票（避免把暂未更新到当天的股票算作差距）
            db_df = pd.read_sql(
                "SELECT DISTINCT symbol FROM daily_bars WHERE date = ?",
                conn, params=(latest_date,),
            )
        except Exception:
            # date 格式不兼容时回退到全量
            try:
                db_df = pd.read_sql("SELECT DISTINCT symbol FROM daily_bars", conn)
            except Exception:
                db_df = pd.DataFrame()
        finally:
            conn.close()

        live_symbols = set(db_df["symbol"].tolist()) if not db_df.empty else set()

        missing = live_symbols - pre_symbols
        missing_by_board = {}
        for sym in sorted(missing):
            board = self._board_tag(sym)
            missing_by_board[board] = missing_by_board.get(board, 0) + 1

        return {
            "precomputed_count": len(pre_symbols),
            "live_count": len(live_symbols),
            "gap_total": len(missing),
            "gap_by_board": missing_by_board,
        }

    # ── 评分模式判定 ──────────────────────────────

    def scoring_mode(self, date: str) -> dict:
        """给定日期 L2 评分将使用哪种路径。"""
        fpath = L2_FACTORS_DIR / f"{date}.parquet"
        if fpath.exists():
            df = pd.read_parquet(fpath)
            return {
                "mode": "precomputed",
                "source": str(fpath),
                "stock_count": len(df),
            }
        return {
            "mode": "live",
            "source": "daily_bars (SQLite 实时计算)",
            "stock_count": None,
        }

    # ── 操作后校验 ──────────────────────────────

    def verify_after_operation(self, op_type: str, **kwargs) -> list[dict]:
        """对指定操作类型，执行对应的校验逻辑。

        Args:
            op_type: "sync_daily" / "precompute_l2" / "signal_update"
                     / "backtest" / "optimization"
            kwargs: 校验所需的上下文（如日期、文件路径等）

        Returns:
            [{"check": str, "status": "ok"|"warn"|"error", "detail": str}, ...]
        """
        results = []
        if op_type == "sync_daily":
            results = self._verify_sync_daily(**kwargs)
        elif op_type == "precompute_l2":
            results = self._verify_precompute_l2(**kwargs)
        elif op_type == "signal_update":
            results = self._verify_signal_update(**kwargs)
        elif op_type == "backtest":
            results = self._verify_backtest(**kwargs)
        elif op_type == "optimization":
            results = self._verify_optimization(**kwargs)
        else:
            results.append({"check": op_type, "status": "error", "detail": f"未知操作类型: {op_type}"})

        for r in results:
            level = r["status"].upper()
            logger.log(
                getattr(logger, level, 20),
                f"[校验] {r['check']}: {r['status']} — {r['detail']}",
            )
        return results

    def _verify_sync_daily(self, **kwargs) -> list[dict]:
        """校验刚下载的 daily_bars parquet 缓存。"""
        from data.quality import validate_download

        results = []
        cache_dir = kwargs.get("cache_dir", "")
        symbols = kwargs.get("symbols", [])
        if not symbols or not cache_dir:
            results.append({"check": "sync_daily_input", "status": "error", "detail": "缺少 symbols 或 cache_dir 参数"})
            return results

        ok, fail = 0, 0
        for sym in symbols:
            fpath = Path(cache_dir) / f"stock_{sym}_daily.parquet"
            if not fpath.exists():
                fail += 1
                continue
            try:
                df = pd.read_parquet(fpath)
                errs = validate_download(df, sym)
                if errs:
                    fail += 1
                else:
                    ok += 1
            except Exception as e:
                fail += 1

        total = len(symbols)
        status = "error" if fail > total * 0.1 else ("warn" if fail > 0 else "ok")
        results.append({
            "check": "daily_bars 缓存校验",
            "status": status,
            "detail": f"{ok}/{total} OK, {fail} FAIL",
        })
        return results

    def _verify_precompute_l2(self, **kwargs) -> list[dict]:
        """校验新生成的预计算因子文件。"""
        results = []
        dates = kwargs.get("dates", [])
        if not dates:
            results.append({"check": "precompute_l2_input", "status": "error", "detail": "缺少 dates 参数"})
            return results

        pre = self.precomputed_status()
        gap = self.universe_gap(as_of_date=pre.get("latest_date"))

        results.append({
            "check": "因子文件生成",
            "status": "ok" if pre.get("available") else "error",
            "detail": f"最新: {pre.get('latest_date', 'N/A')}, {pre.get('total_files', 0)} 个文件",
        })

        gap_total = gap.get("gap_total", 0)
        gap_status = "ok" if gap_total == 0 else ("warn" if gap_total < 500 else "error")
        results.append({
            "check": "因子覆盖率 vs daily_bars",
            "status": gap_status,
            "detail": f"因子 {gap.get('precomputed_count')} / 数据库 {gap.get('live_count')}, 差距 {gap_total} 只"
                       + (f" ({gap.get('gap_by_board')})" if gap_total > 0 else ""),
        })

        # NaN 检测：扫最新 N 个因子文件
        # volume 列为已知的数据源问题（TX 源无 volume），单独检测
        if pre.get("available"):
            files = sorted(L2_FACTORS_DIR.glob("2026-*.parquet"))
            files = [f for f in files if f.stem != "_trade_dates"]
            recent = files[-min(len(files), 5):]
            nan_total, nan_files, vol_nan_total = 0, 0, 0
            for f in recent:
                try:
                    df = pd.read_parquet(f)
                    # 排除 volume 列（已知 TX 源数据问题）
                    cols_no_vol = [c for c in df.columns if c != "volume"]
                    non_vol_nan = df[cols_no_vol].isna().sum().sum()
                    total_cells = df[cols_no_vol].shape[0] * df[cols_no_vol].shape[1]
                    pct = non_vol_nan / total_cells if total_cells > 0 else 0
                    if pct > 0.01:
                        nan_files += 1
                    nan_total += pct
                    # volume 列单独统计
                    if "volume" in df.columns:
                        vol_nan_total += df["volume"].isna().mean()
                except Exception:
                    pass
            if recent:
                avg_nan = nan_total / len(recent)
                avg_vol_nan = vol_nan_total / len(recent) if vol_nan_total > 0 else 0
                nan_status = "ok" if avg_nan < 0.001 else ("warn" if avg_nan < 0.01 else "error")
                detail = f"近 5 日均值: {avg_nan:.4%}（排除 volume）, {nan_files}/{len(recent)} 文件异常"
                if avg_vol_nan > 0.5:
                    detail += f" | volume NaN: {avg_vol_nan:.1%}（已知数据源问题）"
                results.append({
                    "check": "因子文件 NaN 占比",
                    "status": nan_status,
                    "detail": detail,
                })

        return results

    def _verify_signal_update(self, **kwargs) -> list[dict]:
        """校验信号表更新。"""
        results = []
        tables = kwargs.get("tables", ["industry_compare", "hot_stocks"])
        sig = self.signal_freshness()
        for tbl in tables:
            info = sig.get(tbl)
            if info is None:
                continue
            results.append({
                "check": f"信号表 {tbl}",
                "status": info.get("status", "error"),
                "detail": f"最新: {info.get('last_date', 'N/A')}, 滞后 {info.get('stale_days', '?')} 天",
            })
        return results

    def _verify_backtest(self, **kwargs) -> list[dict]:
        """校验回测结果。"""
        results = []
        result = kwargs.get("result", {})
        trades = result.get("trades", [])
        daily_values = result.get("daily_values", [])

        results.append({
            "check": "回测完成",
            "status": "ok",
            "detail": f"交易 {len(trades)} 笔, 日线 {len(daily_values)} 条",
        })

        if daily_values:
            closes = [d.get("total_value", 0) for d in daily_values]
            if any(v <= 0 for v in closes):
                results.append({
                    "check": "资产净值",
                    "status": "error",
                    "detail": "存在非正资产净值",
                })
            dd = [d.get("drawdown", 0) for d in daily_values]
            max_dd = min(dd) if dd else 0
            if max_dd < -0.5:
                results.append({
                    "check": "最大回撤",
                    "status": "warn",
                    "detail": f"最大回撤 {max_dd:.2%} 超过 50%",
                })

        return results

    def _verify_optimization(self, **kwargs) -> dict:
        """校验参数优化结果。"""
        results = []
        params = kwargs.get("params", {})
        for k, v in params.items():
            if v is None or (isinstance(v, float) and np.isnan(v)):
                results.append({
                    "check": f"参数 {k}",
                    "status": "error",
                    "detail": f"值为 {v}，无效",
                })
            elif k == "stop_loss" and isinstance(v, float) and v >= 0:
                results.append({
                    "check": "参数 stop_loss",
                    "status": "warn",
                    "detail": f"stop_loss={v} 应为负值（止损）",
                })
            elif k == "position_size" and isinstance(v, float) and v <= 0:
                results.append({
                    "check": "参数 position_size",
                    "status": "error",
                    "detail": f"position_size={v} 应为正值",
                })
        if not results:
            results.append({
                "check": "参数优化结果",
                "status": "ok",
                "detail": f"{len(params)} 个参数均在合理范围内",
            })
        return results

    # ── 全报告 ──────────────────────────────

    def full_report(self, date: str | None = None) -> dict:
        """汇总所有检查项，生成完整状态报告。

        Args:
            date: 目标日期，用于判断评分模式。默认当前日期。

        Returns:
            dict 包含所有检查结果。
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        return {
            "date": date,
            "scoring_mode": self.scoring_mode(date),
            "precomputed": self.precomputed_status(),
            "signals": self.signal_freshness(),
            "daily_bars": self.daily_bars_coverage(),
            "universe_gap": self.universe_gap(as_of_date=date),
        }

    # ── 内部工具 ──────────────────────────────

    @staticmethod
    def _stale_status(stale_days: int) -> str:
        if stale_days > STALE_ERROR_DAYS:
            return "error"
        if stale_days > STALE_WARN_DAYS:
            return "warn"
        return "ok"

    @staticmethod
    def _board_tag(symbol: str) -> str:
        if symbol.startswith("30"):
            return "创业板(30)"
        if symbol.startswith("60"):
            return "沪主板(60)"
        if symbol.startswith("00"):
            return "深主板(00)"
        if symbol.startswith("688"):
            return "科创板(688)"
        return "其他"


def check_precomputed_consistency() -> list[dict]:
    """快捷入口：检查预计算因子一致性。"""
    checker = DataFreshnessChecker()
    return checker.verify_after_operation("precompute_l2", dates=["latest"])


def check_signal_update(tables: list[str] | None = None) -> list[dict]:
    """快捷入口：检查信号表更新。"""
    checker = DataFreshnessChecker()
    return checker.verify_after_operation("signal_update", tables=tables)
