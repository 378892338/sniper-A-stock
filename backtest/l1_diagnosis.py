"""L1 命中率诊断 — 验证市场环境评估是否真的有预测力

用法: python -m backtest.l1_diagnosis
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

from gate.layer1_market import assess_market
from backtest.data_loader import load_all_from_cache
from core.logger import get_logger

logger = get_logger("backtest.l1_diagnosis")


def main():
    cache_dir = Path("data/raw/_cache/backtest")
    cached = load_all_from_cache(cache_dir, n_stocks=4318)

    market_weekly = cached["market_weekly"]
    market_monthly = cached["market_monthly"]
    benchmark = cached["benchmark"]  # csi300 daily close Series

    # 构建周度日期列表
    weekly_dates = sorted(set().union(
        *[df.index for df in market_weekly.values() if not df.empty]
    ))
    weekly_dates = [d for d in weekly_dates if "2019-01-01" <= str(d)[:10] <= "2026-04-30"]

    records = []
    for w_date in weekly_dates:
        w_str = str(w_date)[:10]
        try:
            w_data = {k: v.loc[:w_str] for k, v in market_weekly.items() if not v.empty and w_str in v.index}
            m_data = {k: v.loc[:w_str] for k, v in market_monthly.items() if not v.empty}
            if len(w_data) < 2:
                continue
            l1 = assess_market(w_data, monthly_data=m_data)

            # 计算未来大盘收益
            fwd_1w, fwd_2w, fwd_4w = [np.nan] * 3
            if benchmark is not None:
                bm = benchmark[benchmark.index >= w_str]
                if len(bm) >= 2:
                    cur_val = bm.iloc[0]
                    for horizon, label in [(5, "1w"), (10, "2w"), (20, "4w")]:
                        if len(bm) > horizon:
                            fut_val = bm.iloc[horizon]
                            ret = float(fut_val / cur_val - 1) if cur_val > 0 else np.nan
                        else:
                            ret = np.nan
                        if label == "1w":
                            fwd_1w = ret
                        elif label == "2w":
                            fwd_2w = ret
                        else:
                            fwd_4w = ret

            records.append({
                "date": w_str,
                "passed": l1.passed,
                "state": l1.market_state,
                "strong_count": l1.strong_count,
                "position_pct": l1.actual_position_pct,
                "fwd_1w": fwd_1w,
                "fwd_2w": fwd_2w,
                "fwd_4w": fwd_4w,
            })
        except Exception as e:
            continue

    df = pd.DataFrame(records)
    df = df.dropna(subset=["fwd_1w"])

    print(f"\n{'='*80}")
    print(f"L1 信号命中率诊断  ({df['date'].iloc[0][:7]} ~ {df['date'].iloc[-1][:7]}, {len(df)} 周)")
    print(f"{'='*80}")

    # ── 1. 信号分布 ──
    print(f"\n[信号分布]")
    state_dist = df["state"].value_counts()
    for s in ["牛市", "震荡", "偏弱", "熊市"]:
        cnt = state_dist.get(s, 0)
        print(f"  {s}: {cnt:>4} 周 ({cnt/len(df)*100:5.1f}%)")
    print(f"  通过率: {df['passed'].mean()*100:.1f}%")

    # ── 2. L1 passed vs 未来N周大盘涨跌 ──
    print(f"\n[择时命中率] L1=通过 → 未来大盘涨? / L1=不通过 → 未来大盘跌?")
    print(f"{'Horizon':>8} {'L1通过时大盘涨%':>18} {'L1不通过时大盘跌%':>20} {'综合命中率':>10}")
    print("-" * 60)

    for horizon, col in [("1 周", "fwd_1w"), ("2 周", "fwd_2w"), ("4 周", "fwd_4w")]:
        df_h = df.dropna(subset=[col])
        if df_h.empty:
            continue

        # L1=passed: 希望市场涨
        passed_mask = df_h["passed"]
        passed_up = (df_h.loc[passed_mask, col] > 0).mean()

        # L1=not passed: 希望市场跌
        not_passed_mask = ~df_h["passed"]
        not_passed_down = (df_h.loc[not_passed_mask, col] < 0).mean()

        # 综合命中率: (L1过+涨) + (L1不过+跌) / total
        total_hit = (
            (passed_mask & (df_h[col] > 0)).sum() +
            (not_passed_mask & (df_h[col] < 0)).sum()
        ) / len(df_h)

        print(f"{horizon:>8} {passed_up:>17.1%} {not_passed_down:>19.1%} {total_hit:>10.1%}")

    # ── 3. 按市场状态分组的未来收益 ──
    print(f"\n[按L1判定状态的未来收益]")
    print(f"{'State':>8} {'次数':>6} {'fwd_1w均值':>12} {'fwd_2w均值':>12} {'fwd_4w均值':>12} {'1w胜率':>8}")
    print("-" * 60)
    for state in ["牛市", "震荡", "偏弱", "熊市"]:
        mask = df["state"] == state
        n = mask.sum()
        if n == 0:
            continue
        sub = df[mask]
        for col in ["fwd_1w", "fwd_2w", "fwd_4w"]:
            sub[col] = pd.to_numeric(sub[col], errors="coerce")
        avg1 = sub["fwd_1w"].mean()
        avg2 = sub["fwd_2w"].mean()
        avg4 = sub["fwd_4w"].mean()
        win1 = (sub["fwd_1w"] > 0).mean()
        print(f"{state:>8} {n:>6} {avg1:>+11.2%} {avg2:>+11.2%} {avg4:>+11.2%} {win1:>7.1%}")

    # ── 4. Strong count vs 未来收益 ──
    print(f"\n[Strong Count 与未来收益]")
    print(f"{'Strong':>8} {'次数':>6} {'fwd_1w均值':>12} {'fwd_4w均值':>12} {'1w胜率':>8}")
    print("-" * 50)
    for sc in range(4):
        mask = df["strong_count"] == sc
        n = mask.sum()
        if n == 0:
            continue
        sub = df[mask]
        avg1 = sub["fwd_1w"].mean()
        avg4 = sub["fwd_4w"].mean()
        win1 = (sub["fwd_1w"] > 0).mean()
        print(f"{sc:>8} {n:>6} {avg1:>+11.2%} {avg4:>+11.2%} {win1:>7.1%}")

    # ── 5. 逐年命中率 ──
    print(f"\n[逐年 L1 命中率]")
    df["year"] = pd.to_datetime(df["date"]).dt.year
    print(f"{'Year':>6} {'周数':>5} {'通过率':>8} {'综合命中率':>10} {'通过时涨%':>10} {'不通过时跌%':>12}")
    print("-" * 60)
    for y in sorted(df["year"].unique()):
        sub = df[df["year"] == y]
        n = len(sub)
        pass_rate = sub["passed"].mean()
        passed_up = (sub.loc[sub["passed"], "fwd_1w"] > 0).mean() if sub["passed"].sum() > 0 else np.nan
        not_passed_down = (sub.loc[~sub["passed"], "fwd_1w"] < 0).mean() if (~sub["passed"]).sum() > 0 else np.nan
        total_hit = (
            (sub["passed"] & (sub["fwd_1w"] > 0)).sum() +
            (~sub["passed"] & (sub["fwd_1w"] < 0)).sum()
        ) / n
        print(f"{y:>6} {n:>5} {pass_rate:>7.1%} {total_hit:>10.1%} "
              f"{passed_up:>9.1%}" if not np.isnan(passed_up) else f"{'N/A':>10} "
              f"{not_passed_down:>11.1%}" if not np.isnan(not_passed_down) else f"{'N/A':>12}")

    # ── 6. 关键结论 ──
    print(f"\n{'='*80}")
    print(f"[诊断结论]")
    print(f"{'='*80}")

    # L1通过时1周胜率
    passed_1w_win = (df.loc[df["passed"], "fwd_1w"] > 0).mean()
    not_passed_1w_down = (df.loc[~df["passed"], "fwd_1w"] < 0).mean()

    # 随机基准: 无条件涨跌概率
    unconditional_up = (df["fwd_1w"] > 0).mean()
    unconditional_down = (df["fwd_1w"] < 0).mean()

    print(f"  无条件1周涨概率: {unconditional_up:.1%}")
    print(f"  L1=通过时 涨概率: {passed_1w_win:.1%} (vs 无条件 {unconditional_up:.1%})")
    print(f"  L1=不通过 跌概率: {not_passed_1w_down:.1%} (vs 无条件 {unconditional_down:.1%})")

    if passed_1w_win > unconditional_up + 0.05:
        print(f"  → L1 做多信号有正向预测力 (+{passed_1w_win - unconditional_up:.1%})")
    elif passed_1w_win < unconditional_up - 0.05:
        print(f"  → L1 做多信号是反向指标 ({passed_1w_win - unconditional_up:.1%})")
    else:
        print(f"  → L1 做多信号无显著预测力 ({passed_1w_win - unconditional_up:+.1%})")

    if not_passed_1w_down > unconditional_down + 0.05:
        print(f"  → L1 空仓信号有正向预测力 (+{not_passed_1w_down - unconditional_down:.1%})")
    elif not_passed_1w_down < unconditional_down - 0.05:
        print(f"  → L1 空仓信号是反向指标 ({not_passed_1w_down - unconditional_down:.1%})")
    else:
        print(f"  → L1 空仓信号无显著预测力 ({not_passed_1w_down - unconditional_down:+.1%})")

    # 逐年稳定性
    yearly_hits = []
    for y in sorted(df["year"].unique()):
        sub = df[df["year"] == y]
        hit = ((sub["passed"] & (sub["fwd_1w"] > 0)).sum() +
               (~sub["passed"] & (sub["fwd_1w"] < 0)).sum()) / len(sub)
        yearly_hits.append(hit)

    if len(yearly_hits) >= 4:
        hit_mean = np.mean(yearly_hits)
        hit_std = np.std(yearly_hits)
        print(f"\n  逐年命中率: 均值 {hit_mean:.1%}, 标准差 {hit_std:.1%}")
        if hit_std > 0.08:
            print(f"  → 命中率年份波动大，L1 稳定性差")
        if all(h > 0.50 for h in yearly_hits):
            print(f"  → 所有年份命中率>50%，L1 跨周期有效")
        else:
            bad_years = [y for y, h in zip(sorted(df["year"].unique()), yearly_hits) if h <= 0.50]
            print(f"  → 命中率≤50%年份: {bad_years}")


if __name__ == "__main__":
    main()
