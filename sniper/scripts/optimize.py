"""参数优化扫描 — 完整参数空间 + 并行化 + 结果缓存"""

import sys
import time
import itertools
import hashlib
import json
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics
from core.logger import get_logger

logger = get_logger("sniper.scripts.optimize")


def _params_hash(params: dict) -> str:
    """参数组生成唯一哈希。"""
    params_str = json.dumps(params, sort_keys=True)
    return hashlib.md5(params_str.encode()).hexdigest()[:8]


def run_param_set(params: dict, start: str = "2019-01-01",
                  end: str = "2026-05-08") -> dict:
    """用一组参数运行回测并返回绩效。

    独立进程安全：每次重建 BacktestEngine + 应用参数覆盖。
    """
    import sniper.config as cfg

    # 配置参数名 → 配置类映射
    CONFIG_MAP: dict[str, tuple[str, type]] = {
        # EXIT config
        "stop_loss": ("EXIT", cfg.ExitConfig),
        "trailing_stop": ("EXIT", cfg.ExitConfig),
        "max_hold_days": ("EXIT", cfg.ExitConfig),
        "ma_break_below": ("EXIT", cfg.ExitConfig),
        # RISK config
        "position_size": ("RISK", cfg.RiskConfig),
        "max_positions": ("RISK", cfg.RiskConfig),
        "max_daily_loss": ("RISK", cfg.RiskConfig),
        "max_total_loss": ("RISK", cfg.RiskConfig),
        "min_hold_days": ("RISK", cfg.RiskConfig),
        # MARKET config (L0)
        "bullish_threshold": ("MARKET", cfg.MarketConfig),
        "bearish_threshold": ("MARKET", cfg.MarketConfig),
        # ENTRY config (L3)
        "soft_min_score": ("ENTRY", cfg.EntryConfig),
        "soft_sector_top": ("ENTRY", cfg.EntryConfig),
        # Removed: max_positions_bull/neutral (not real config keys), l2_* (StockConfig weights)
    }

    # 分组应用覆盖
    originals = {}
    updates: dict[str, dict] = {}
    for key, val in params.items():
        if key not in CONFIG_MAP:
            continue
        cfg_name, cfg_cls = CONFIG_MAP[key]
        if cfg_name not in updates:
            updates[cfg_name] = {}
            originals[cfg_name] = getattr(cfg, cfg_name)
        updates[cfg_name][key] = val

    try:
        for cfg_name, kv in updates.items():
            cfg_cls = CONFIG_MAP[next(k for k, v in CONFIG_MAP.items() if v[0] == cfg_name)][1]
            current = originals[cfg_name]
            setattr(cfg, cfg_name, cfg_cls(**{**current.__dict__, **kv}))

        engine = BacktestEngine()
        result = engine.run(start_date=start, end_date=end)
        if not result:
            return {}
        metrics = calculate_metrics(
            result.get("daily_values", []),
            result.get("trades", []),
            cfg.BACKTEST.initial_capital,
        )
        metrics["name"] = "_".join(f"{k}={v}" for k, v in params.items())
        metrics["params"] = params
        metrics["params_hash"] = _params_hash(params)
        metrics["trades_count"] = len([t for t in result.get("trades", []) if t.get("action") == "BUY"])
        return metrics
    finally:
        # 恢复原始配置
        for cfg_name, original in originals.items():
            setattr(cfg, cfg_name, original)


def main():
    """完整参数空间网格搜索 + 并行化。"""
    logger.info("=" * 60)
    logger.info("参数优化扫描 — 完整参数空间")
    logger.info("=" * 60)

    param_grid = {
        # 风控参数
        "stop_loss": [-0.05, -0.08, -0.10, -0.12, -0.15],
        "position_size": [0.08, 0.10, 0.12, 0.15, 0.20],
        "max_hold_days": [10, 15, 20, 30],
        "trailing_stop": [-0.03, -0.05, -0.08],

        # 市场状态参数
        "bullish_threshold": [55, 60, 65],
        "bearish_threshold": [35, 40, 45],

        # L2 因子权重
        "l2_trend_weight": [0.10, 0.15, 0.20],
        "l2_volume_weight": [0.05, 0.10, 0.15],
        "l2_macd_weight": [0.05, 0.10, 0.15],
        "l2_fund_flow_weight": [0.10, 0.15, 0.20],

        # L3 入场过滤
        "soft_min_score": [55, 60, 65, 70, 75],
        "soft_sector_top": [2, 3, 5],

        # 最大持仓
        "max_positions_bull": [3, 5, 7],
        "max_positions_neutral": [2, 3, 5],
    }

    # 计算总组合数
    keys = list(param_grid.keys())
    param_combos = list(itertools.product(*param_grid.values()))
    total = len(param_combos)
    logger.info(f"参数空间: {total} 组合 ({len(keys)} 个参数)")

    # 并行执行
    import multiprocessing
    n_workers = min(multiprocessing.cpu_count(), 8)
    logger.info(f"并行 workers: {n_workers}")

    results = []
    start_time = time.time()

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(run_param_set, dict(zip(keys, vals))): dict(zip(keys, vals))
            for vals in param_combos
        }
        for i, future in enumerate(as_completed(futures), 1):
            params = futures[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
                    logger.info(f"[{i}/{total}] Sharpe={result.get('sharpe', 0):.2f} "
                               f"年化={result.get('annual_return', 0):.2%} "
                               f"回撤={result.get('max_drawdown', 0):.2%}")
                else:
                    logger.warning(f"[{i}/{total}] 回测无结果: {params}")
            except Exception as e:
                logger.warning(f"[{i}/{total}] 失败: {e}")

    elapsed = time.time() - start_time
    logger.info(f"优化完成: {len(results)}/{total} 组合成功, 耗时 {elapsed:.0f}s")

    if results:
        print("\n" + "=" * 90)
        print(f"{'参数组合':^40s} {'Sharpe':>8s} {'年化':>8s} {'回撤':>8s} {'胜率':>8s} {'交易':>6s}")
        print("-" * 90)
        for r in sorted(results, key=lambda x: x.get("sharpe", -999), reverse=True)[:15]:
            print(f"{r.get('name', ''):40s} {r.get('sharpe', 0):8.2f} "
                  f"{r.get('annual_return', 0):8.2%} {r.get('max_drawdown', 0):8.2%} "
                  f"{r.get('win_rate', 0):8.2%} {r.get('trades_count', 0):6d}")
        print("=" * 90)

        # 保存结果
        output_path = project_root / "outputs" / "optimize_results.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        logger.info(f"结果已保存: {output_path}")


if __name__ == "__main__":
    main()
