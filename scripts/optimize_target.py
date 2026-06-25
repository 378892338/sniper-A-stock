"""打靶归因 v5 — 打字机模式（一次采样 + 纸带输出）

流程:
  1. 30 组随机参数并行跑回测
  2. 每笔交易附上市场指纹 {l0_score, l0_trend, l0_volume, l0_breadth}
  3. 输出 paper_tape.parquet（纸带）
  4. 运行时每天 configure_for_today() 在纸带上找最近邻 → 归因 → 出参数

打字机模式下不在此处做归因，只做采样和纸带输出。
归因在 config.py 的 configure_for_today() 中实时完成。
"""

import sys, json, gc, logging, os, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from core.logger import get_logger


logger = get_logger("scripts.optimize_target")
OUTPUT_DIR = Path("outputs/optimize_target"); OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_fh = logging.FileHandler(str(OUTPUT_DIR / "optimize.log"), encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s | %(message)s"))
logger.addHandler(_fh)

from sniper.config import _PARAMS_META as PARAM_META


def run(params: dict, start: str, end: str) -> dict:
    """单次回测（带市场指纹采集）。

    和之前一样的回测逻辑，但额外采集 L0 各子维度评分。
    返回 not just trades, but also daily l0_scores for the whole period.
    """
    import sniper.config as cfg
    snap = {}
    for n in ["EXIT", "RISK", "ENTRY", "MARKET"]:
        snap[n] = getattr(cfg, n)
    try:
        for keys, config_cls, config_name in [
            ({"stop_loss", "trailing_stop", "max_hold_days"}, cfg.ExitConfig, "EXIT"),
            ({"position_size"}, cfg.RiskConfig, "RISK"),
            ({"soft_min_score"}, cfg.EntryConfig, "ENTRY"),
            ({"bullish_threshold"}, cfg.MarketConfig, "MARKET"),
        ]:
            ov = {k: v for k, v in params.items() if k in keys}
            if ov:
                setattr(cfg, config_name, config_cls(**{**getattr(cfg, config_name).__dict__, **ov}))

        from sniper.engine.backtest import BacktestEngine
        from sniper.engine.metrics import calculate_metrics
        r = BacktestEngine().run(start, end, use_precomputed=True)
        m = calculate_metrics(r.get("daily_values", []), r.get("trades", []), 1_000_000)
        result = {"m": m, "trades": r.get("trades", []), "daily_l0_scores": r.get("l0_scores", {})}
        del r
        gc.collect()
        return result
    finally:
        for n, v in snap.items():
            setattr(cfg, n, v)


def extract_trades(paired: list) -> list:
    """将每笔交易附上参数快照 + 市场指纹。

    返回: [{
        "params": {...},
        "pnl_pct": float,
        "l0_score": float,
        "l0_trend": float,
        "l0_volume": float,
        "l0_breadth": float,
        "market_state_vector": [l0, trend, volume, breadth],
        "entry_date": str,
        "exit_date": str,
        "hold_days": int,
        "exit_reason": str,
    }, ...]
    """
    tx = []
    n_skipped_no_pnl = 0
    n_skipped_no_entry = 0
    n_skipped_buy = 0
    for p, res in paired:
        for t in res.get("trades", []):
            if t.get("action") != "SELL":
                n_skipped_buy += 1
                continue
            if t.get("pnl") is None:
                n_skipped_no_pnl += 1
                continue
            t = t.copy()
            # pnl_pct: 优先取回测引擎计算的，取不到则从 pnl/cost 自算
            pnl_pct = t.get("pnl_pct")
            if pnl_pct is None:
                cost = t.get("cost", 0) or 0
                pnl_abs = t.get("pnl", 0) or 0
                pnl_pct = pnl_abs / cost if cost > 0 else 0.0
            else:
                pnl_pct = pnl_pct or 0.0
            entry_date = t.get("entry_date", "")
            if not entry_date:
                n_skipped_no_entry += 1
                continue

            # hold_days：从 entry/exit date 计算（回测引擎中 self_evolve=False 时不设）
            exit_date = t.get("date", "")
            hold_days = 0
            if exit_date:
                try:
                    ed = datetime.datetime.strptime(entry_date, "%Y-%m-%d")
                    xd = datetime.datetime.strptime(exit_date, "%Y-%m-%d")
                    hold_days = max(1, (xd - ed).days)
                except ValueError:
                    logger.warning(f"日期解析失败: entry={entry_date}, exit={exit_date}, symbol={t.get('symbol','')}")

            # 从回测结果中取该入场日期的L0各维度评分
            daily_l0 = res.get("daily_l0_scores", {})
            l0_info = daily_l0.get(entry_date, {})
            l0_score = l0_info.get("composite", 50.0)
            l0_trend = l0_info.get("trend", 50.0)
            l0_volume = l0_info.get("volume", 50.0)
            l0_breadth = l0_info.get("breadth", 50.0)

            tx.append({
                "params": p,
                "pnl_pct": pnl_pct,
                "l0_score": l0_score,
                "l0_trend": l0_trend,
                "l0_volume": l0_volume,
                "l0_breadth": l0_breadth,
                "market_state_vector": [l0_score, l0_trend, l0_volume, l0_breadth],
                "entry_date": entry_date,
                "exit_date": t.get("date", ""),
                "hold_days": hold_days,
                "exit_reason": t.get("reason", ""),
                "symbol": t.get("symbol", ""),
            })
    if n_skipped_buy or n_skipped_no_pnl or n_skipped_no_entry:
        logger.info(f"交易过滤: BUY={n_skipped_buy}, 无PnL={n_skipped_no_pnl}, 无入场日期={n_skipped_no_entry}")
    return tx


from sniper.config import _snap_to_grid


_RAW_RESULTS_FILE = OUTPUT_DIR / "raw_results.pkl"


def _save_raw_results(combos, results):
    """保存回测原始结果到 pickle，支持 --re-extract 跳过回测。"""
    import pickle
    _RAW_RESULTS_FILE.write_bytes(pickle.dumps({"combos": combos, "results": results}))
    logger.info(f"原始回测结果已保存: {_RAW_RESULTS_FILE}")


def _load_raw_results():
    """加载保存的原始结果。"""
    import pickle
    if _RAW_RESULTS_FILE.exists():
        try:
            data = pickle.loads(_RAW_RESULTS_FILE.read_bytes())
        except Exception as e:
            logger.error(f"原始结果文件损坏或格式不兼容: {e}")
            logger.error(f"请删除 {_RAW_RESULTS_FILE} 后重新正常采样")
            return None, None
        logger.info(f"已加载 {len(data['results'])} 组原始回测结果")
        return data["combos"], data["results"]
    logger.error(f"原始结果不存在，请先正常采样: {_RAW_RESULTS_FILE}")
    return None, None


def sample(start="2025-01-01", end="2026-06-04", n_samples=30, workers=None,
           re_extract=False):
    """打字机初始化采样。

    和之前最大的区别:
      1. 只跑一轮（不分轮次）
      2. 每笔交易带市场指纹（4维向量）
      3. 输出 paper_tape.parquet（不是 result.json）
      4. 归因在运行时实时做（不在此处做）

    Args:
        re_extract: 为 True 时跳过回测，从保存的 raw_results.pkl 重新提取纸带。
    """
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, as_completed

    # ── 重提取模式：跳过回测 ──
    if re_extract:
        logger.info(f"{'='*60}")
        logger.info(f"打字机纸带重提取模式（跳过回测）")
        logger.info(f"{'='*60}")
        combos, results = _load_raw_results()
        if combos is None:
            return
        _build_paper_tape(combos, results)
        return

    n_workers = workers or min(3, max(1, mp.cpu_count() - 2))
    ctx = mp.get_context("spawn")

    logger.info(f"{'='*60}")
    logger.info(f"打字机初始化采样")
    logger.info(f"  区间: {start} ~ {end}")
    logger.info(f"  参数数: {len(PARAM_META)}")
    logger.info(f"  采样组数: {n_samples}")
    logger.info(f"  并行进程: {n_workers}")
    logger.info(f"{'='*60}")

    # 生成随机参数组合
    combos = []
    for _ in range(n_samples):
        p = {}
        for k, meta in PARAM_META.items():
            v = np.random.uniform(meta["lo"], meta["hi"])
            p[k] = _snap_to_grid(v, meta)
        combos.append(p)

    logger.info(f"已生成 {len(combos)} 组随机参数")

    # 并行跑回测
    results = [None] * len(combos)
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as ex:
        fut_map = {ex.submit(run, p, start, end): i for i, p in enumerate(combos)}
        for f in as_completed(fut_map):
            i = fut_map[f]
            try:
                results[i] = f.result(timeout=600)
            except Exception as e:
                logger.warning(f"  [{i+1}] 失败: {e}")
                results[i] = {"m": {"sharpe": -999, "total_return": 0}, "trades": [], "daily_l0_scores": {}}

    # 保存原始回测结果（支持 --re-extract 秒级修复）
    _save_raw_results(combos, results)

    # 构建纸带
    _build_paper_tape(combos, results)


def _build_paper_tape(combos: list, results: list) -> dict | None:
    """从回测结果构建纸带 parquet + 元数据。

    与 sample() 分离以支持 --re-extract 跳过回测。
    """
    valid = [(p, r) for p, r in zip(combos, results) if r is not None]
    for i, (p, r) in enumerate(valid):
        m = r["m"]
        logger.info(f"  [{i+1}/{len(valid)}] Sharpe={m.get('sharpe',0):.2f} "
                    f"收益={m.get('total_return',0):.2%} 交易={m.get('total_trades',0)}")

    # 提取交易（带市场指纹）
    all_trades = extract_trades(valid)
    logger.info(f"总交易数: {len(all_trades)} 笔")

    if len(all_trades) < 10:
        logger.error("交易样本不足（<10 笔），终止")
        raise RuntimeError(f"交易样本不足: 仅 {len(all_trades)} 笔（需要 >=10 笔）")

    # 输出纸带（保存所有交易原始记录）
    tape_path = OUTPUT_DIR / "paper_tape.parquet"
    df = pd.DataFrame(all_trades)
    # 展平 params dict 为多列
    params_expanded = df["params"].apply(pd.Series)
    params_expanded.columns = [f"param_{c}" for c in params_expanded.columns]
    # 展平 market_state_vector 为多列
    vector_df = df["market_state_vector"].apply(pd.Series)
    vector_df.columns = ["msv_l0", "msv_trend", "msv_volume", "msv_breadth"]

    df_flat = pd.concat([
        df.drop(columns=["params", "market_state_vector"]),
        params_expanded,
        vector_df,
    ], axis=1)
    df_flat.to_parquet(tape_path, index=False)
    logger.info(f"纸带已写入: {tape_path} ({len(df_flat)} 笔交易)")

    # 保存参数组合明细（用于复现）
    (OUTPUT_DIR / "run_params.json").write_text(
        json.dumps(combos, ensure_ascii=False, indent=2)
    )
    logger.info(f"参数组合已写入: {OUTPUT_DIR / 'run_params.json'}")

    # 保存元数据
    meta = {
        "version": "V4.0",
        "generated_at": pd.Timestamp.now().isoformat(),
        "backtest_start": "",  # 重提取模式无此信息
        "backtest_end": "",
        "n_samples": len(combos),
        "n_valid": len(valid),
        "n_trades": len(all_trades),
        "optimized_params": list(PARAM_META.keys()),
    }
    (OUTPUT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    logger.info(f"元数据已写入: {OUTPUT_DIR / 'meta.json'}")

    # 打印归因结果（仅供参考，不做为线上依赖）
    logger.info(f"\n{'='*60}")
    logger.info(f"纸带包含 {len(all_trades)} 笔交易")
    logger.info(f"运行时将实时归因 —— 打字机模式自进化")
    logger.info(f"{'='*60}")
    return meta


if __name__ == "__main__":
    import argparse
    a = argparse.ArgumentParser()
    a.add_argument("--start", default="2025-01-01")
    a.add_argument("--end", default="2026-06-04")
    a.add_argument("--workers", type=int, default=0, help="并行进程数, 0=auto")
    a.add_argument("--n-samples", type=int, default=30, help="随机参数组数")
    a.add_argument("--re-extract", action="store_true", help="跳过回测，从 raw_results.pkl 重新提取纸带")
    a.add_argument("--seed", type=int, default=None, help="随机种子，用于复现采样")
    args = a.parse_args()
    if args.seed is not None:
        np.random.seed(args.seed)
    w = args.workers if args.workers > 0 else None
    sample(start=args.start, end=args.end, n_samples=args.n_samples, workers=w,
           re_extract=args.re_extract)
