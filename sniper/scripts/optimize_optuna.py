"""参数优化 — Optuna 贝叶斯优化 + 缓存 + 参数敏感性分析"""

import sys
import time
import json
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import optuna
from optuna.samplers import TPESampler

from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

logger = get_logger("sniper.scripts.optimize_optuna")

STUDY_DIR = project_root / "outputs" / "optuna_studies"
STUDY_DIR.mkdir(parents=True, exist_ok=True)


def build_params(trial: optuna.Trial) -> dict:
    """从 Optuna Trial 采样参数。"""
    return {
        # 风控参数
        "stop_loss": trial.suggest_float("stop_loss", -0.20, -0.03, step=0.01),
        "trailing_stop": trial.suggest_float("trailing_stop", -0.10, -0.02, step=0.01),
        "max_hold_days": trial.suggest_int("max_hold_days", 5, 60),
        "position_size": trial.suggest_float("position_size", 0.05, 0.25, step=0.01),

        # 市场状态参数
        "bullish_threshold": trial.suggest_int("bullish_threshold", 50, 75),
        "bearish_threshold": trial.suggest_int("bearish_threshold", 30, 50),

        # L3 入场过滤
        "soft_min_score": trial.suggest_int("soft_min_score", 50, 85),
        "soft_sector_top": trial.suggest_int("soft_sector_top", 1, 8),
    }


def objective(trial: optuna.Trial, start: str = "2019-01-01",
              end: str = "2026-05-08") -> float:
    """Optuna 目标函数。返回 Sharpe 比率。"""
    params = build_params(trial)

    # 应用参数覆盖
    import sniper.config as cfg

    # 保存原始配置
    orig_exit = cfg.EXIT
    orig_risk = cfg.RISK

    try:
        cfg.EXIT = cfg.ExitConfig(**{
            "stop_loss": params["stop_loss"],
            "trailing_stop": params["trailing_stop"],
            "max_hold_days": params["max_hold_days"],
            "ma_break_below": cfg.EXIT.ma_break_below,
        })
        cfg.RISK = cfg.RiskConfig(**{
            "max_positions": cfg.RISK.max_positions,
            "position_size": params["position_size"],
            "max_sector_exposure": cfg.RISK.max_sector_exposure,
            "max_daily_loss": cfg.RISK.max_daily_loss,
            "max_total_loss": cfg.RISK.max_total_loss,
            "min_hold_days": cfg.RISK.min_hold_days,
        })

        engine = BacktestEngine()
        result = engine.run(start_date=start, end_date=end, params=params, cache=True)
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

        # 多目标：最大化 Sharpe，最小化回撤
        # 如果回撤 > 30%，重罚
        if dd > 0.30:
            sharpe -= 2.0

        trial.set_user_attr("max_drawdown", dd)
        trial.set_user_attr("params", params)

        return sharpe

    finally:
        cfg.EXIT = orig_exit
        cfg.RISK = orig_risk


def print_best(study: optuna.Study, elapsed: float):
    """打印最优参数。"""
    best = study.best_trial
    print("\n" + "=" * 70)
    print(f"  贝叶斯优化完成: {len(study.trials)} 次迭代, 耗时 {elapsed:.0f}s")
    print("=" * 70)
    print(f"  最佳 Sharpe:   {best.value:.4f}")
    print(f"  最大回撤:     {best.user_attrs.get('max_drawdown', 0):.2%}")
    print(f"  ── 最优参数 ──")
    for key, val in best.params.items():
        print(f"  {key:25s}: {val}")
    print("=" * 70)


def run_optimization(n_trials: int = 200, start: str = "2019-01-01",
                     end: str = "2026-05-08", parallel: bool = True):
    """运行贝叶斯优化。"""
    study_name = f"optimize_{start}_{end}".replace("-", "")
    storage_path = str(STUDY_DIR / f"{study_name}.db")
    storage_url = f"sqlite:///{storage_path}"

    # 加载或创建 study
    try:
        study = optuna.create_study(
            study_name=study_name,
            storage=storage_url,
            load_if_exists=True,
            direction="maximize",
            sampler=TPESampler(seed=42, multivariate=True),
        )
        logger.info(f"Study '{study_name}' 已加载: {len(study.trials)} 次已有试验")
    except Exception:
        study = optuna.create_study(
            study_name=study_name,
            direction="maximize",
            sampler=TPESampler(seed=42, multivariate=True),
        )
        logger.info(f"Study '{study_name}' 新建")

    start_time = time.time()

    try:
        study.optimize(
            lambda trial: objective(trial, start, end),
            n_trials=n_trials,
            n_jobs=-1 if parallel else 1,
            show_progress_bar=True,
            gc_after_trial=True,
        )
    except KeyboardInterrupt:
        logger.info("用户中断优化")

    elapsed = time.time() - start_time

    print_best(study, elapsed)

    # 参数重要性分析
    try:
        fig = optuna.visualization.plot_param_importances(study)
        fig.write_html(str(STUDY_DIR / f"{study_name}_importances.html"))
        logger.info(f"参数重要性已保存: {STUDY_DIR}/{study_name}_importances.html")
    except Exception:
        pass

    return study


def continue_optimization(n_more: int = 100):
    """继续上一次优化。"""
    study_name = f"optimize_20190101_20260508"
    storage_path = str(STUDY_DIR / f"{study_name}.db")
    storage_url = f"sqlite:///{storage_path}"

    try:
        study = optuna.load_study(
            study_name=study_name,
            storage=storage_url,
            sampler=TPESampler(seed=42, multivariate=True),
        )
        logger.info(f"继续优化: 已有 {len(study.trials)} 次, 追加 {n_more} 次")
        study.optimize(
            lambda trial: objective(trial, "2019-01-01", "2026-05-08"),
            n_trials=n_more,
            gc_after_trial=True,
        )
        print_best(study, 0)
    except Exception as e:
        logger.error(f"继续优化失败: {e}")


def show_best():
    """显示当前最优参数。"""
    study_name = f"optimize_20190101_20260508"
    storage_path = str(STUDY_DIR / f"{study_name}.db")
    storage_url = f"sqlite:///{storage_path}"

    try:
        study = optuna.load_study(
            study_name=study_name,
            storage=storage_url,
        )
        print("\n" + "=" * 70)
        print(f"  Study: {study_name}")
        print(f"  已运行试验: {len(study.trials)}")
        print(f"  最佳试验: #{study.best_trial.number}")
        print(f"  最佳 Sharpe: {study.best_value:.4f}")
        print(f"  最优参数:")
        for k, v in study.best_params.items():
            print(f"    {k}: {v}")
        print("=" * 70)
    except Exception as e:
        print(f"无法加载 study: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=200, help="试验次数")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-05-08")
    parser.add_argument("--continue", dest="continue_opt", action="store_true")
    parser.add_argument("--show", action="store_true", help="显示当前最优参数")
    args = parser.parse_args()

    if args.show:
        show_best()
    elif args.continue_opt:
        continue_optimization(100)
    else:
        run_optimization(args.trials, args.start, args.end)
