"""3 策略全量回测对比 — Baseline vs 策略A vs 策略B

不修改任何现有代码，仅通过替换全局 config 实现策略切换。
"""

import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import sniper.config as CFG
from sniper.engine.backtest import BacktestEngine
from sniper.engine.metrics import calculate_metrics

YEARS = [
    ("2019", "2019-01-01", "2019-12-31"),
    ("2020", "2020-01-01", "2020-12-31"),
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026", "2026-01-01", "2026-06-09"),
]

# ── 3 组策略定义 ──
STRATEGIES = {
    "baseline": {
        "desc": "当前默认: L0>=70开仓, 板块L0>=75取5/70-75取3, 仓位50%",
        "config": {},
    },
    "strategy_a": {
        "desc": "L0>=64开仓, 板块统一前3, L0[64-70)仓位30%/>=70仓位50%",
        "config": {
            "MARKET": {"bullish_threshold": 64},
            "SECTOR": {"top_n_high": 3, "top_n_low": 3},
        },
        "dynamic_exposure": True,  # 用 monkeypatch 实现
    },
    "strategy_b": {
        "desc": "L0>=70开仓(不变), 板块统一前3, 仓位50%",
        "config": {
            "SECTOR": {"top_n_high": 3, "top_n_low": 3},
        },
    },
}


def apply_config(overrides: dict):
    """安全替换全局 config（frozen dataclass → 新实例）。"""
    M = {
        "MARKET": CFG.MarketConfig,
        "SECTOR": CFG.SectorConfig,
        "RISK": CFG.RiskConfig,
        "ENTRY": CFG.EntryConfig,
        "EXIT": CFG.ExitConfig,
    }
    for name, cls in M.items():
        ov = overrides.get(name)
        if ov is None:
            continue
        old = getattr(CFG, name)
        setattr(CFG, name, cls(**{**old.__dict__, **ov}))


SNAPSHOT = {}  # 全局快照


def save_snapshot():
    global SNAPSHOT
    SNAPSHOT = {
        "MARKET": CFG.MARKET,
        "SECTOR": CFG.SECTOR,
        "RISK": CFG.RISK,
        "ENTRY": CFG.ENTRY,
        "EXIT": CFG.EXIT,
    }


def restore_snapshot():
    for name, obj in SNAPSHOT.items():
        setattr(CFG, name, obj)


def monthly_from_daily_values(daily_values: list[dict]) -> dict:
    """从 daily_values 算每月收益。"""
    if not daily_values:
        return {}
    import pandas as pd
    df = pd.DataFrame(daily_values)
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.strftime("%Y-%m")
    monthly = {}
    for month, group in df.groupby("month", sort=True):
        vals = group["total_value"].values
        if len(vals) >= 2:
            ret = vals[-1] / vals[0] - 1
            monthly[month] = round(float(ret * 100), 2)
        elif len(vals) == 1:
            monthly[month] = 0.0
    return monthly


def analyze_trades(trades):
    sells = [t for t in trades
             if t.get("action") == "SELL" and t.get("pnl") is not None]
    pnls = np.array([t["pnl"] for t in sells], dtype=float) if sells else np.array([])
    return {
        "n_sells": len(sells),
        "win_rate": round(float((pnls > 0).mean()), 3) if len(pnls) > 0 else 0,
        "avg_pnl": round(float(pnls.mean()), 0) if len(pnls) > 0 else 0,
        "median_pnl": round(float(np.median(pnls)), 0) if len(pnls) > 0 else 0,
        "best_trade": round(float(pnls.max()), 0) if len(pnls) > 0 else 0,
        "worst_trade": round(float(pnls.min()), 0) if len(pnls) > 0 else 0,
        "total_pnl_sum": round(float(pnls.sum()), 0) if len(pnls) > 0 else 0,
    }


def _setup_dynamic_exposure(engine):
    """策略 A: 按 L0 分层的动态仓位 monkeypatch。

    核心思路: BacktestEngine 的 _l0_cache 在 precompute 阶段已存好每天 L0 评分。
    用 max key 取最新 L0，决定 target_exposure_ratio。
    """
    orig_fn = engine.risk.get_remaining_budget

    def dynamic_get_budget():
        # 从 engine._l0_cache 取当前交易日对应的 L0
        current_l0 = 70.0
        if engine._l0_cache:
            latest_date = max(engine._l0_cache.keys())
            l0_info = engine._l0_cache[latest_date]
            if isinstance(l0_info, dict):
                current_l0 = l0_info.get("composite", 70.0)
            else:
                current_l0 = float(l0_info)
        target = 0.50 if current_l0 >= 70 else 0.30
        return max(0.0, engine.risk.total_capital * target - engine.risk.total_exposure)

    engine.risk.get_remaining_budget = dynamic_get_budget


def run_one_strategy(label, desc, config_overrides, dynamic_exposure=False):
    """跑一组策略的全量回测。"""
    save_snapshot()
    apply_config(config_overrides)
    if dynamic_exposure:
        # 先设一个基础 target_exposure，后续由 monkeypatch 覆盖
        apply_config({"RISK": {"target_exposure_ratio": 0.50}})

    years_data = []
    for yr, start, end in YEARS:
        engine = BacktestEngine()
        if dynamic_exposure:
            _setup_dynamic_exposure(engine)

        result = engine.run(start, end, use_precomputed=True, self_evolve=False)
        trades = result.get("trades", [])
        daily_values = result.get("daily_values", [])
        m = calculate_metrics(daily_values, trades, 1_000_000) if daily_values else {}
        ta = analyze_trades(trades)
        monthly = monthly_from_daily_values(daily_values)

        years_data.append({
            "year": yr,
            "total_return": round(m.get("total_return", 0) * 100, 2),
            "annual_return": round(m.get("annual_return", 0) * 100, 2),
            "max_drawdown": round(m.get("max_drawdown", 0) * 100, 2),
            "sharpe": round(m.get("sharpe", 0), 2),
            **ta,
            "monthly": monthly,
        })

    restore_snapshot()

    full_compound = round(float(np.prod(
        [1 + y["total_return"] / 100 for y in years_data]
    ) - 1) * 100, 2)

    return {"label": label, "desc": desc, "years": years_data,
            "full_total": full_compound}


def print_comparison(all_results):
    """打印三组策略对比。"""
    print("\n" + "=" * 160)
    print("  三策略全量回测对比 (2019-2026)")
    print("=" * 160)

    # 表头
    strategies = [r["label"] for r in all_results]

    # 年度逐行
    fmt_year = "| {:>4} ".format
    fmt_val  = "| {:>12} {:>8} {:>7} {:>6} {:>6} {:>6} {:>6} {:>9} {:>9} |".format

    for yr_name, _, _ in YEARS:
        print(f"\n  === {yr_name} ===")
        print(f"| {'策略':>12} | {'总收益':>8} | {'年化':>7} | {'回撤':>6} | {'夏普':>6} | {'SELL':>6} | {'胜率':>6} | {'平均PnL':>9} | {'累计PnL':>9} |")
        print(f"|{'-'*12}|{'-'*10}|{'-'*9}|{'-'*8}|{'-'*8}|{'-'*8}|{'-'*8}|{'-'*11}|{'-'*11}|")
        for r in all_results:
            yd = next(y for y in r["years"] if y["year"] == yr_name)
            print(
                f"| {r['label']:>12} "
                f"| {yd['total_return']:>7.2f}% "
                f"| {yd['annual_return']:>6.2f}% "
                f"| {yd['max_drawdown']:>5.2f}% "
                f"| {yd['sharpe']:>5.2f} "
                f"| {yd['n_sells']:>5} "
                f"| {yd['win_rate']:>5.1%} "
                f"| {yd['avg_pnl']:>+8.0f} "
                f"| {yd['total_pnl_sum']:>+8.0f} |"
            )

    # 全周期汇总
    print(f"\n\n  {'='*80}")
    print(f"  全周期汇总")
    print(f"  {'='*80}")
    print(f"| {'策略':>12} | {'累计收益':>8} | {'年化':>7} | {'夏普均值':>7} | {'回撤均值':>7} | {'总SELL':>7} | {'总PnL':>9} |")
    print(f"|{'-'*12}|{'-'*10}|{'-'*9}|{'-'*9}|{'-'*9}|{'-'*9}|{'-'*11}|")
    for r in all_results:
        sharps = [y["sharpe"] for y in r["years"] if y["n_sells"] > 0]
        dds = [y["max_drawdown"] for y in r["years"]]
        total_sells = sum(y["n_sells"] for y in r["years"])
        total_pnl = sum(y["total_pnl_sum"] for y in r["years"])
        annualized = round(
            (1 + r["full_total"] / 100) ** (1 / 8) - 1, 4
        ) * 100
        print(
            f"| {r['label']:>12} "
            f"| {r['full_total']:>7.2f}% "
            f"| {annualized:>6.2f}% "
            f"| {float(np.mean(sharps)):>6.2f} "
            f"| {float(np.mean(dds)):>6.2f}% "
            f"| {total_sells:>6} "
            f"| {total_pnl:>+8.0f} |"
        )

    # ── 逐月对比 ──
    print(f"\n\n  {'='*140}")
    print(f"  逐月收益对比")
    print(f"  {'='*140}")
    for yr_name, _, _ in YEARS:
        print(f"\n  === {yr_name} 逐月收益 ===")
        all_months = set()
        yr_data = {}
        for r in all_results:
            yd = next(y for y in r["years"] if y["year"] == yr_name)
            if yd.get("monthly"):
                all_months.update(yd["monthly"].keys())
                yr_data[r["label"]] = yd["monthly"]
        if not all_months:
            continue
        all_months = sorted(all_months)
        # 表头
        hdr = f"| {'月份':>7}"
        for r in all_results:
            hdr += f" | {r['label']:>11}"
        hdr += " |"
        print(hdr)
        print("|" + "-" * (len(hdr) - 2) + "|")
        for m in all_months:
            line = f"| {m[5:]:>7}"
            for r in all_results:
                val = yr_data.get(r["label"], {}).get(m, None)
                if val is not None:
                    line += f" | {val:>+10.2f}%"
                else:
                    line += f" | {'—':>11}"
            line += " |"
            print(line)
        # 年累计行
        line = f"| {'年累计':>7}"
        for r in all_results:
            yd = next(y for y in r["years"] if y["year"] == yr_name)
            line += f" | {yd['total_return']:>+10.2f}%"
        line += " |"
        print(line)
    print()


def main():
    print("开始 3 策略全量回测对比...")

    all_results = []
    for label, info in STRATEGIES.items():
        print(f"\n{'='*60}")
        print(f"  [{label}] {info['desc']}")
        print(f"{'='*60}")
        result = run_one_strategy(
            label, info["desc"], info["config"],
            info.get("dynamic_exposure", False),
        )
        all_results.append(result)
        print(f"  >> 完成: 全周期累计 {result['full_total']:.2f}%")

    print_comparison(all_results)

    out = Path("outputs/strategy_comparison.json")
    out.write_text(json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"JSON 已保存: {out}")


if __name__ == "__main__":
    main()
