"""Strategy A 参数优化

以当前 Strategy A 配置为 baseline，对关键参数做 Optuna 贝叶斯搜索。
优化目标为最大化 Sharpe 比率（含回撤惩罚）。

优化参数:
  - stop_loss: 初始止损幅度
  - trailing_stop: 动态止盈回撤幅度
  - max_hold_days: 最大持仓天数
  - soft_min_score: L2 入场最低评分

注意: position_size 不在此优化（Strategy A 由 L0 档位动态预算决定）。
bullish_threshold 固定为 64 不参与优化。

并行策略（Windows-safe）:
  1. 主进程预计算 L0/L1 缓存 → 存 parquet
  2. multiprocessing.Pool 3 workers 独立跑回测
  3. workers 各自 import cfg（无竞争）
  4. 主进程 study.tell() 写 SQLite（无锁死）
"""

import sys, time, json, os
import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import optuna
import sniper.config as cfg
from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

logger = get_logger("scripts.optimize_strategy_a")

# ── Strategy A 基准配置 ──
BASE_MARKET = dict(cfg.MarketConfig(bullish_threshold=64).__dict__)
BASE_ENTRY = dict(cfg.EntryConfig().__dict__)
BASE_EXIT = dict(cfg.ExitConfig().__dict__)
BASE_RISK = dict(cfg.RiskConfig().__dict__)

# 搜索空间定义（只包含对 Strategy A 有意义的参数）
# position_size 不参与优化：Strategy A 仓位由 L0 档位动态预算决定
SEARCH_SPACE = {
    "stop_loss": {"lo": -0.10, "hi": -0.02, "step": 0.01},
    "trailing_stop": {"lo": -0.10, "hi": -0.02, "step": 0.01},
    "max_hold_days": {"lo": 5, "hi": 60, "step": 5},
    "soft_min_score": {"lo": 55, "hi": 85, "step": 2},
}

TODAY = datetime.datetime.now().strftime("%Y-%m-%d")
STUDY_DIR = Path("outputs/optuna_studies")
L0L1_CACHE_DIR = STUDY_DIR / "l0_l1_cache"


def _get_available_mem_mb() -> int:
    """获取可用物理内存（MB）。未安装 psutil 时返回一个大数，不限制 workers。"""
    if not _HAS_PSUTIL:
        return 99999
    return int(psutil.virtual_memory().available / 1024**2)


# ──────────────────────────────────────────────
# L0/L1 预计算缓存（跨 trial 共享，一次算终身用）
# ──────────────────────────────────────────────

def _ensure_cache(start: str, end: str) -> None:
    """主进程一次性预计算不依赖策略参数的缓存。

    缓存的 3 类数据均独立于 stop_loss/trailing_stop 等优化参数，
    各子进程从缓存加载，跳过 1801 天重复计算。

    包含：
      - L0/L1 评分缓存（parquet，约 1800 天）
      - L2 因子内存缓存（pickle，替代 1801 次 parquet 读取）
    """
    import pickle
    import pandas as pd
    L0L1_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    l0_marker = L0L1_CACHE_DIR / "l0.parquet"
    factor_marker = L0L1_CACHE_DIR / "factors.pkl"

    if l0_marker.exists() and factor_marker.exists():
        logger.info(f"全量缓存已存在: {L0L1_CACHE_DIR}")
        return

    logger.info("预计算全量缓存（一次性，后续 trials 跳过）...")
    engine = BacktestEngine()
    cal = engine.router.get_trading_dates(start, end)
    dates = sorted(cal["date"].tolist())

    # ── 1. L0/L1 缓存 ──
    if not l0_marker.exists():
        engine._precompute_l0_l1(dates)
        l0_records = []
        for date_str, score_dict in engine._l0_cache.items():
            rec = {"date": date_str}
            rec.update(score_dict)
            l0_records.append(rec)
        l0_df = pd.DataFrame(l0_records)
        l0_df.to_parquet(L0L1_CACHE_DIR / "l0.parquet", index=False)

        l1_records = []
        for date_str, sector_list in engine._l1_cache.items():
            l1_records.append({"date": date_str, "sectors": json.dumps(sector_list)})
        l1_df = pd.DataFrame(l1_records)
        l1_df.to_parquet(L0L1_CACHE_DIR / "l1.parquet", index=False)

        l1_full_parts = []
        for date_str, df_date in engine._l1_df_cache.items():
            if df_date is not None and not df_date.empty:
                part = df_date.copy()
                part["_date"] = date_str
                l1_full_parts.append(part)
        if l1_full_parts:
            l1_full_df = pd.concat(l1_full_parts, ignore_index=True)
            l1_full_df.to_parquet(L0L1_CACHE_DIR / "l1_full.parquet", index=False)
        logger.info(f"L0/L1 缓存已保存 ({len(l0_records)} 天)")

    # ── 2. L2 因子缓存（pickle，避免每个 worker 重读 1801 个 parquet）──
    if not factor_marker.exists():
        engine.stock_scorer._load_factor_cache(dates)
        if engine.stock_scorer._factor_cache_all:
            with open(factor_marker, "wb") as f:
                pickle.dump(engine.stock_scorer._factor_cache_all, f)
            logger.info(f"L2 因子缓存已保存 ({len(engine.stock_scorer._factor_cache_all)} 天)")

    logger.info("全量缓存就绪")


# ──────────────────────────────────────────────
# Worker 函数（在子进程中运行）
# ──────────────────────────────────────────────

def _worker_trial(trial_number: int, params: dict) -> dict:
    """在子进程中运行单次回测。

    完全独立于主进程的状态：
      - 子进程重新 import sniper.config（独立副本，无竞争）
      - 从 parquet 加载 L0/L1 缓存（避免重复计算）

    Args:
        trial_number: 用于日志标识的 trial 编号
        params: 此 trial 的参数字典
    """
    # 子进程重新 import（Windows spawn 模式下必须）
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import logging
    trial_logger = logging.getLogger(f"worker.trial.{trial_number}")

    import pandas as pd
    import sniper.config as ccfg
    from sniper.engine.backtest import BacktestEngine
    from sniper.engine.metrics import calculate_metrics

    # ── 加载 L0/L1 缓存 ──
    l0_cache = {}
    l0_df = pd.read_parquet(L0L1_CACHE_DIR / "l0.parquet")
    for _, row in l0_df.iterrows():
        d = row.to_dict()
        date_str = d.pop("date")
        l0_cache[date_str] = d

    l1_cache = {}
    l1_df = pd.read_parquet(L0L1_CACHE_DIR / "l1.parquet")
    for _, row in l1_df.iterrows():
        l1_cache[row["date"]] = json.loads(row["sectors"])

    l1_full_cache = {}
    l1_full_path = L0L1_CACHE_DIR / "l1_full.parquet"
    if l1_full_path.exists():
        l1_full_df = pd.read_parquet(l1_full_path)
        for date_str, grp in l1_full_df.groupby("_date"):
            l1_full_cache[date_str] = grp.drop(columns=["_date"])

    # ── 加载 L2 因子缓存（pickle，避免重复读 1801 个 parquet）──
    import pickle
    factor_cache_path = L0L1_CACHE_DIR / "factors.pkl"
    factor_cache = None
    if factor_cache_path.exists():
        with open(factor_cache_path, "rb") as f:
            factor_cache = pickle.load(f)

    # ── 应用参数到本进程的 cfg 副本 ──
    exit_kwargs = dict(BASE_EXIT)
    if "stop_loss" in params: exit_kwargs["stop_loss"] = params["stop_loss"]
    if "trailing_stop" in params: exit_kwargs["trailing_stop"] = params["trailing_stop"]
    if "max_hold_days" in params: exit_kwargs["max_hold_days"] = params["max_hold_days"]
    ccfg.EXIT = ccfg.ExitConfig(**exit_kwargs)

    risk_kwargs = dict(BASE_RISK)
    ccfg.RISK = ccfg.RiskConfig(**risk_kwargs)

    entry_kwargs = dict(BASE_ENTRY)
    if "soft_min_score" in params: entry_kwargs["soft_min_score"] = params["soft_min_score"]
    ccfg.ENTRY = ccfg.EntryConfig(**entry_kwargs)

    ccfg.MARKET = ccfg.MarketConfig(**BASE_MARKET)

    # ── 跑回测 ──
    engine = BacktestEngine()
    # 注入 L2 因子缓存 → 跳过 _load_factor_cache() 的 1801 次 parquet 读取
    if factor_cache is not None:
        engine.stock_scorer._factor_cache_all = factor_cache

    # ── bars 缓存：1 次批量 SQL 替代 ~9 万次 SQL/trial ──
    # L3/L4 每天对每只候选/持仓全量拉取日线（只取 close/volume 等 3-4 个字段）
    # 缓存后：1 次 SELECT * FROM daily_bars WHERE symbol IN (...) → 内存 O(1) 查找
    stock_list_df = engine.router.get_stock_list()
    if stock_list_df is not None and not stock_list_df.empty:
        engine._preload_bars_cache(stock_list_df["symbol"].tolist())
        # Monkey-patch: L3 check_hard / L4 evaluate 走缓存而非 SQL
        _orig_get_bars = engine.router.get_daily_bars
        def _cached_get_bars(symbol, start=None, end=None):
            df = engine._bars_cache.get(symbol)
            if df is not None:
                if end is not None:
                    df = df[df.index <= pd.Timestamp(end)]
                if start is not None:
                    df = df[df.index >= pd.Timestamp(start)]
                if not df.empty:
                    return df.reset_index()
            return _orig_get_bars(symbol, start=start, end=end)
        engine.router.get_daily_bars = _cached_get_bars

    result = engine.run(
        start_date="2019-01-01", end_date=TODAY,
        use_precomputed=True, self_evolve=False,
        l0_cache=l0_cache, l1_cache=l1_cache, l1_full_cache=l1_full_cache,
    )
    if not result:
        trial_logger.warning(f"Trial #{trial_number} FAILED: engine.run returned empty")
        return {"sharpe": -999.0, "max_drawdown": 1.0, "total_return": 0, "n_trades": 0}

    metrics = calculate_metrics(
        result.get("daily_values", []),
        result.get("trades", []),
        ccfg.BACKTEST.initial_capital,
    )
    if not metrics:
        trial_logger.warning(f"Trial #{trial_number} FAILED: calculate_metrics returned empty")
        return {"sharpe": -999.0, "max_drawdown": 1.0, "total_return": 0, "n_trades": 0}

    sharpe = metrics.get("sharpe", -999.0)
    dd = metrics.get("max_drawdown", 1.0)

    # 回撤 > 15% → 重罚
    if dd > 0.15:
        sharpe -= 3.0
    elif dd > 0.10:
        sharpe -= 1.0

    trial_logger.info(
        f"Trial #{trial_number} completed: "
        f"sharpe={sharpe:.4f} max_drawdown={dd:.4f} "
        f"total_return={metrics.get('total_return', 0):.4f} "
        f"n_trades={len(result.get('trades', []))}"
    )

    return {
        "sharpe": sharpe,
        "max_drawdown": dd,
        "total_return": metrics.get("total_return", 0),
        "n_trades": len(result.get("trades", [])),
    }


# ──────────────────────────────────────────────
# 单进程 Objective（保留用于 n_jobs=1 的兼容）
# ──────────────────────────────────────────────

def apply_params(params: dict) -> dict:
    """应用参数到全局 config，返回旧值快照用于恢复。"""
    snap = {
        "EXIT": cfg.EXIT,
        "RISK": cfg.RISK,
        "ENTRY": cfg.ENTRY,
        "MARKET": cfg.MARKET,
    }
    exit_kwargs = dict(BASE_EXIT)
    if "stop_loss" in params: exit_kwargs["stop_loss"] = params["stop_loss"]
    if "trailing_stop" in params: exit_kwargs["trailing_stop"] = params["trailing_stop"]
    if "max_hold_days" in params: exit_kwargs["max_hold_days"] = params["max_hold_days"]
    cfg.EXIT = cfg.ExitConfig(**exit_kwargs)

    risk_kwargs = dict(BASE_RISK)
    cfg.RISK = cfg.RiskConfig(**risk_kwargs)

    entry_kwargs = dict(BASE_ENTRY)
    if "soft_min_score" in params: entry_kwargs["soft_min_score"] = params["soft_min_score"]
    cfg.ENTRY = cfg.EntryConfig(**entry_kwargs)

    # bullish_threshold 固定 64，不参与优化
    cfg.MARKET = cfg.MarketConfig(**BASE_MARKET)

    return snap


def restore_config(snap: dict):
    for k, v in snap.items():
        setattr(cfg, k, v)


def objective(trial: optuna.Trial, start: str = "2019-01-01", end: str = TODAY) -> float:
    """Optuna 目标函数：最大化 Sharpe（含回撤惩罚）。

    此函数用于单进程模式（n_jobs=1）。
    与 _worker_trial 一样，加载预计算缓存避免每次从头算 L0/L1。
    """
    params = {}
    for key, meta in SEARCH_SPACE.items():
        if isinstance(meta["lo"], int):
            params[key] = trial.suggest_int(key, meta["lo"], meta["hi"], step=meta["step"])
        else:
            params[key] = trial.suggest_float(key, meta["lo"], meta["hi"], step=meta.get("step", None))

    snap = apply_params(params)
    try:
        import pandas as pd
        import pickle
        import json

        # ── 加载 L0/L1/L2 缓存（一次性，避免每个 trial 重复算 1803 天）──
        l0_cache = {}
        l0_df = pd.read_parquet(L0L1_CACHE_DIR / "l0.parquet")
        for _, row in l0_df.iterrows():
            d = row.to_dict()
            date_str = d.pop("date")
            l0_cache[date_str] = d

        l1_cache = {}
        l1_df = pd.read_parquet(L0L1_CACHE_DIR / "l1.parquet")
        for _, row in l1_df.iterrows():
            l1_cache[row["date"]] = json.loads(row["sectors"])

        l1_full_cache = {}
        l1_full_path = L0L1_CACHE_DIR / "l1_full.parquet"
        if l1_full_path.exists():
            l1_full_df = pd.read_parquet(l1_full_path)
            for date_str, grp in l1_full_df.groupby("_date"):
                l1_full_cache[date_str] = grp.drop(columns=["_date"])

        factor_cache_path = L0L1_CACHE_DIR / "factors.pkl"
        factor_cache = None
        if factor_cache_path.exists():
            with open(factor_cache_path, "rb") as f:
                factor_cache = pickle.load(f)

        engine = BacktestEngine()
        if factor_cache is not None:
            engine.stock_scorer._factor_cache_all = factor_cache

        # ── bars 缓存 ──
        stock_list_df = engine.router.get_stock_list()
        if stock_list_df is not None and not stock_list_df.empty:
            engine._preload_bars_cache(stock_list_df["symbol"].tolist())
            _orig_get_bars = engine.router.get_daily_bars
            def _cached_get_bars(symbol, start=None, end=None):
                df = engine._bars_cache.get(symbol)
                if df is not None:
                    if end is not None:
                        df = df[df.index <= pd.Timestamp(end)]
                    if start is not None:
                        df = df[df.index >= pd.Timestamp(start)]
                    if not df.empty:
                        return df.reset_index()
                return _orig_get_bars(symbol, start=start, end=end)
            engine.router.get_daily_bars = _cached_get_bars

        result = engine.run(
            start_date=start, end_date=end,
            use_precomputed=True, self_evolve=False,
            l0_cache=l0_cache, l1_cache=l1_cache, l1_full_cache=l1_full_cache,
        )
        if not result:
            return -999.0

        metrics = calculate_metrics(
            result.get("daily_values", []),
            result.get("trades", []),
            cfg.BACKTEST.initial_capital,
        )
        if not metrics:
            return -999.0

        sharpe = metrics.get("sharpe", -999.0)
        dd = metrics.get("max_drawdown", 1.0)

        if dd > 0.15:
            sharpe -= 3.0
        elif dd > 0.10:
            sharpe -= 1.0

        trial.set_user_attr("max_drawdown", dd)
        trial.set_user_attr("total_return", metrics.get("total_return", 0))
        trial.set_user_attr("params", params)
        trial.set_user_attr("n_trades", len(result.get("trades", [])))

        return sharpe
    finally:
        restore_config(snap)


# ──────────────────────────────────────────────
# 并行运行（Windows-safe multiprocessing.Pool）
# ──────────────────────────────────────────────

def run_optimization_parallel(n_trials: int = 80) -> optuna.Study:
    """使用 multiprocessing.Pool 并行跑回测。

    Windows 兼容方案：子进程独立 import cfg，主进程 study.tell() 写 DB。
    """
    STUDY_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. 预计算全量缓存（L0/L1/L2因子/日线，一次性）──
    _ensure_cache("2019-01-01", TODAY)

    # ── 2. 创建 study ──
    study_name = "strategy_a_opt"
    storage_path = str(STUDY_DIR / f"{study_name}.db")
    storage_url = f"sqlite:///{storage_path}"

    try:
        study = optuna.create_study(
            study_name=study_name, storage=storage_url,
            load_if_exists=True, direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42, multivariate=True),
        )
        logger.info(f"Study 加载: {len(study.trials)} 次已有试验")
    except Exception:
        study = optuna.create_study(
            study_name=study_name, direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42, multivariate=True),
        )
        logger.info("Study 新建")

    # 只计数已完成的试验（跳过 RUNNING：上次中断残留的 ask()）
    n_existing = sum(1 for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE)
    n_remaining = n_trials - n_existing
    if n_remaining <= 0:
        logger.info(f"已有 {n_existing} 次完成试验，无需新增")
        print_results(study, 0)
        return study

    # ── 3. 收集 params：优先恢复残留 RUNNING trials ──
    running_trials = [t for t in study.trials if t.state != optuna.trial.TrialState.COMPLETE]
    if running_trials:
        logger.info(f"检测到 {len(running_trials)} 个残留 RUNNING trials，启动恢复模式")
        all_params: list[tuple[int, dict]] = sorted(
            [(t.number, t.params) for t in running_trials]
        )
        _trial_id_map: dict[int, int] = {
            t.number: t._trial_id for t in running_trials
        }
        logger.info(f"恢复 trials: {[tn for tn, _ in all_params]}")
    else:
        logger.info(f"已有 {n_existing} 次完成试验，需新增 {n_remaining} 次")
        all_params = []
        _trial_id_map = {}
        for _ in range(n_remaining):
            trial = study.ask()
            params = {}
            for key, meta in SEARCH_SPACE.items():
                if isinstance(meta["lo"], int):
                    params[key] = trial.suggest_int(key, meta["lo"], meta["hi"], step=meta["step"])
                else:
                    params[key] = trial.suggest_float(key, meta["lo"], meta["hi"], step=meta.get("step", None))
            _trial_id_map[trial.number] = trial._trial_id
            all_params.append((trial.number, params))

    # 保存 params 字典供 writeback 使用（恢复模式下需要记 params 以存入 user_attrs）
    all_params_dict = {tn: p for tn, p in all_params}

    # ── 4. Pool 并行跑回测 ──
    # 注意：Windows spawn 模式下每个 worker 独立加载全部数据（~1GB/worker），
    # 2 workers 并发峰值超过系统承受 → 固定 1 worker。
    # 但如果用了 psutil 且内存非常充裕（>8GB），可以尝试 2 workers。
    avail_mem = _get_available_mem_mb()
    n_workers = 2 if avail_mem > 12000 else 1
    if n_workers > 1:
        logger.info(f"可用内存 {avail_mem}MB 充裕，使用 {n_workers} workers")
    else:
        logger.info(f"可用内存 {avail_mem}MB，使用单 worker 避免 OOM")
    logger.info(f"并行回测: {n_workers} workers, {len(all_params)} trials")

    start_time = time.time()

    with __import__("multiprocessing").Pool(processes=n_workers, maxtasksperchild=1) as pool:
        async_results = [
            pool.apply_async(_worker_trial, (tn, param_dict))
            for tn, param_dict in all_params
        ]

        for i, ar in enumerate(async_results):
            tn = all_params[i][0]
            try:
                result = ar.get(timeout=7200)  # 2h timeout per trial
            except Exception as e:
                logger.error(f"Trial #{tn} 失败: {e}")
                result = {"sharpe": -999.0, "max_drawdown": 1.0,
                          "total_return": 0, "n_trades": 0}

            # 单个 trial 完成后立即写入 study（持久化），而非等全部结束
            try:
                tid = _trial_id_map[tn]
                study._storage.set_trial_user_attr(tid, "max_drawdown", result["max_drawdown"])
                study._storage.set_trial_user_attr(tid, "total_return", result["total_return"])
                study._storage.set_trial_user_attr(tid, "n_trades", result["n_trades"])
                study._storage.set_trial_user_attr(tid, "params", str(all_params_dict[tn]))
                study.tell(tn, result["sharpe"])
                logger.info(f"Trial #{tn} 结果已写入: sharpe={result['sharpe']:.4f}")
            except Exception as e:
                logger.error(f"Trial #{tn} study.tell 失败: {e}")

            if (i + 1) % 10 == 0 or i == len(all_params) - 1:
                elapsed = time.time() - start_time
                done = i + 1
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(all_params) - done) / rate if rate > 0 else 0
                logger.info(f"进度: {done}/{len(all_params)}, "
                            f"耗时 {elapsed:.0f}s, "
                            f"预估剩余 {eta:.0f}s")

    elapsed = time.time() - start_time
    logger.info(f"并行优化完成: {len(all_params)} trials, 耗时 {elapsed:.0f}s")

    print_results(study, elapsed)

    # 参数重要性图
    try:
        fig = optuna.visualization.plot_param_importances(study)
        fig.write_html(str(STUDY_DIR / "strategy_a_importances.html"))
    except Exception:
        pass

    return study


def print_results(study: optuna.Study, elapsed: float):
    """打印优化结果。"""
    if not study.trials:
        print("\nStudy 中没有已完成试验")
        return
    best = study.best_trial
    print("\n" + "=" * 70)
    print(f"  贝叶斯优化完成: {len(study.trials)} 次迭代, 耗时 {elapsed:.0f}s")
    print(f"  最佳试验 #{best.number}")
    print("=" * 70)
    print(f"  最佳 Sharpe:     {best.value:.4f}")
    print(f"  对应总收益:     {best.user_attrs.get('total_return', 0)*100:.2f}%")
    print(f"  对应回撤:       {best.user_attrs.get('max_drawdown', 0)*100:.2f}%")
    print(f"  对应交易数:     {best.user_attrs.get('n_trades', 0)}")
    print(f"\n  ── 最优参数 ──")
    # 对比当前值
    print(f"  {'参数':25s} {'最优值':>10} {'当前值':>10} {'变化':>10}")
    print(f"  {'-'*25} {'-'*10} {'-'*10} {'-'*10}")
    for key in sorted(SEARCH_SPACE.keys()):
        opt_val = best.params.get(key, "—")
        cur_val = {
            "stop_loss": cfg.EXIT.stop_loss,
            "trailing_stop": cfg.EXIT.trailing_stop,
            "max_hold_days": cfg.EXIT.max_hold_days,
            "soft_min_score": cfg.ENTRY.soft_min_score,
            # bullish_threshold 固定 64 不优化，此处不对比
        }.get(key, "?")
        change = ""
        if isinstance(opt_val, (int, float)) and isinstance(cur_val, (int, float)):
            diff = opt_val - cur_val
            change = f"{'+' if diff > 0 else ''}{diff:.2f}"
        print(f"  {key:25s} {str(opt_val):>10} {str(cur_val):>10} {change:>10}")

    # 参数重要性
    try:
        importances = optuna.importance.get_param_importances(study)
        print(f"\n  ── 参数重要性排名 ──")
        for k, v in sorted(importances.items(), key=lambda x: -x[1]):
            print(f"  {k:25s}: {v:.3f}")
    except Exception:
        pass

    print("=" * 70)

    # 保存结果
    out_path = STUDY_DIR / "best_params.json"
    out_data = {
        "sharpe": best.value,
        "max_drawdown": best.user_attrs.get("max_drawdown", 0),
        "total_return": best.user_attrs.get("total_return", 0),
        "params": best.params,
        "n_trials": len(study.trials),
    }
    out_path.write_text(json.dumps(out_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  最优参数已保存: {out_path}")


def run_optimization(n_trials: int = 150):
    """运行优化。"""
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    study_name = "strategy_a_opt"
    storage_path = str(STUDY_DIR / f"{study_name}.db")
    storage_url = f"sqlite:///{storage_path}"

    try:
        study = optuna.create_study(
            study_name=study_name, storage=storage_url,
            load_if_exists=True, direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42, multivariate=True),
        )
        logger.info(f"Study 加载: {len(study.trials)} 次已有试验")
    except Exception:
        study = optuna.create_study(
            study_name=study_name, direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42, multivariate=True),
        )
        logger.info("Study 新建")

    start_time = time.time()

    # ── 预计算 L0/L1/L2 缓存（一次性，每个 trial 共享）──
    _ensure_cache("2019-01-01", TODAY)

    try:
        study.optimize(
            lambda trial: objective(trial, "2019-01-01", TODAY),
            n_trials=n_trials,
            n_jobs=1,
            show_progress_bar=True,
            gc_after_trial=True,
        )
    except KeyboardInterrupt:
        logger.info("用户中断")

    elapsed = time.time() - start_time
    print_results(study, elapsed)

    # 参数重要性图
    try:
        fig = optuna.visualization.plot_param_importances(study)
        fig.write_html(str(STUDY_DIR / "strategy_a_importances.html"))
    except Exception:
        pass

    return study


if __name__ == "__main__":
    import argparse
    a = argparse.ArgumentParser()
    a.add_argument("--trials", type=int, default=150,
                    help="总试验次数（并行模式默认80，单进程默认150）")
    a.add_argument("--show", action="store_true", help="显示现有最优参数")
    a.add_argument("--parallel", action="store_true",
                    help="多进程并行（Windows-safe, 推荐80 trials）")
    args = a.parse_args()

    if args.show:
        STUDY_DIR.mkdir(parents=True, exist_ok=True)
        study = optuna.create_study(
            study_name="strategy_a_opt",
            storage=f"sqlite:///{STUDY_DIR}/strategy_a_opt.db",
            load_if_exists=True,
            direction="maximize",
        )
        print_results(study, 0)
    elif args.parallel:
        run_optimization_parallel(args.trials)
    else:
        run_optimization(args.trials)
