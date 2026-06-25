"""回测诊断引擎 — 全链路中间状态捕获、持久化与查询。

每次回测运行自动保存各层级中间结果到 parquet，提供离线查询能力，
无需重跑即可定位问题。

保存的数据:
  market_state.parquet   — L0: 每日市场状态评分与分类
  sector_scores.parquet  — L1: 每周板块动量和广度评分
  weekly_l4_pool.parquet — L2: 每周L4池候选股
  entry_funnel.parquet   — L3: 每只候选股各条件通过/失败
  trades.parquet         — L4: 交易明细
  nav.parquet            — 净值曲线
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# ═══════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════

@dataclass
class EntryFunnelRecord:
    """记录一只候选股在入场检查中各条件的通过情况。

    字段对应新 L3 双层过滤：
      - 第一层（硬性）: ma250_pass
      - 第二层（5条件至少4/5）: ma_momentum, ma20_pass, near_ma20, dif_gt_0, vol_ma44_confirm
    """
    date: str
    symbol: str
    sector: str = ""
    in_holdings: bool = False
    same_week_lock: bool = False
    has_ms: bool = True
    has_data: bool = True
    ma20_ready: bool = False
    ma20_pass: bool = False
    ma250_ready: bool = False
    ma250_pass: bool = False
    # 新L3 5条件
    ma_momentum: bool = False      # MA20 > MA60
    near_ma20: float = 0.0         # abs(close/MA20-1)，不追高偏离度
    dif_gt_0: bool = False         # DIF > 0
    vol_ma44_confirm: bool = False # vol ≥ MA(vol, 44)
    confirm_count: int = 0         # 5条件中通过数
    sector_match: bool = False
    final_pass: bool = False
    fail_reason: str = ""


# ═══════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════

DIAGNOSE_BASE_DIR = Path("data/raw/_cache/backtest/diagnose")


def _funnel_df(funnel: list[EntryFunnelRecord]) -> pd.DataFrame:
    return pd.DataFrame([vars(r) for r in funnel])


def _trades_df(trades: list) -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": t.symbol,
        "entry_date": str(t.entry_date)[:10],
        "exit_date": str(t.exit_date)[:10],
        "entry_price": float(t.entry_price),
        "exit_price": float(t.exit_price),
        "net_return": float(t.net_return),
        "holding_days": int(t.holding_days),
        "exit_reason": t.exit_reason,
        "sector": t.sector,
    } for t in trades])


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def make_run_id(params: dict | None = None) -> str:
    """生成唯一运行 ID，格式: YYYYMMDD_HHMMSS_nstocks."""
    n = params.get("n_stocks", "N") if params else "N"
    return f"{_now_str()}_{n}"


def save_run(
    run_id: str,
    base_dir: str | Path = DIAGNOSE_BASE_DIR,
    params: dict | None = None,
    market_state: pd.Series | None = None,
    sector_momentum: dict | None = None,
    sector_breadth: dict | None = None,
    weekly_l4_pool: dict | None = None,
    weekly_sectors: dict | None = None,
    entry_funnel: list[EntryFunnelRecord] | None = None,
    trades: list | None = None,
    nav: pd.Series | None = None,
    benchmark_return: float | None = None,
) -> Path:
    """保存一次运行的完整诊断数据到 parquet。"""
    run_dir = Path(base_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    saved = []

    # 参数
    with open(run_dir / "params.json", "w") as f:
        json.dump({k: str(v) if not isinstance(v, (int, float, bool, type(None))) else v
                   for k, v in (params or {}).items()},
                  f, indent=2)

    # 市场状态
    if market_state is not None and not market_state.empty:
        df = market_state.to_frame("state")
        df.index.name = "date"
        df.to_parquet(run_dir / "market_state.parquet")
        saved.append("market_state")

    # 板块评分
    if sector_momentum or sector_breadth:
        rows = []
        sectors = set(list(sector_momentum or {})) | set(list(sector_breadth or {}))
        for sec in sectors:
            rows.append({"sector": sec, "metric": "momentum",
                         **pd.Series(sector_momentum.get(sec, {})).to_dict()})
            rows.append({"sector": sec, "metric": "breadth",
                         **pd.Series(sector_breadth.get(sec, {})).to_dict()})
        if rows:
            df = pd.DataFrame(rows)
            df.to_parquet(run_dir / "sector_scores.parquet")
            saved.append("sector_scores")

    # 每周 L4 池
    if weekly_l4_pool:
        rows = [{"week": wk, "symbol": sym, "rank": i}
                for wk, syms in weekly_l4_pool.items()
                for i, sym in enumerate(syms)]
        pd.DataFrame(rows).to_parquet(run_dir / "weekly_l4_pool.parquet")
        saved.append("weekly_l4_pool")

    # 每周强势板块
    if weekly_sectors:
        rows = [{"week": wk, "sector": sec, "rank": i}
                for wk, sectors in weekly_sectors.items()
                for i, sec in enumerate(sectors)]
        pd.DataFrame(rows).to_parquet(run_dir / "weekly_sectors.parquet")
        saved.append("weekly_sectors")

    # 入场漏斗
    if entry_funnel:
        _funnel_df(entry_funnel).to_parquet(run_dir / "entry_funnel.parquet")
        saved.append("entry_funnel")

    # 交易明细
    if trades:
        _trades_df(trades).to_parquet(run_dir / "trades.parquet")
        saved.append("trades")

    # 净值
    if nav is not None and not nav.empty:
        df = nav.to_frame("nav")
        df.index.name = "date"
        df.to_parquet(run_dir / "nav.parquet")
        saved.append("nav")

    # 清单
    with open(run_dir / "manifest.json", "w") as f:
        json.dump({
            "run_id": run_id,
            "created_at": _now_str(),
            "saved": saved,
            "benchmark_return": benchmark_return,
        }, f, indent=2)

    return run_dir


def load_run(run_id: str, base_dir: str | Path = DIAGNOSE_BASE_DIR) -> dict[str, Any]:
    """加载一次运行的所有诊断数据。"""
    run_dir = Path(base_dir) / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"运行 {run_id} 不存在于 {run_dir}")

    data: dict[str, Any] = {"run_id": run_id}

    # 清单
    mf = run_dir / "manifest.json"
    if mf.exists():
        with open(mf) as f:
            data["manifest"] = json.load(f)

    # 参数
    pf = run_dir / "params.json"
    if pf.exists():
        with open(pf) as f:
            data["params"] = json.load(f)

    # 数据表
    for name in ["market_state", "sector_scores", "weekly_l4_pool",
                  "weekly_sectors", "entry_funnel", "trades", "nav"]:
        f = run_dir / f"{name}.parquet"
        if f.exists():
            data[name] = pd.read_parquet(f)

    return data


def list_runs(base_dir: str | Path = DIAGNOSE_BASE_DIR) -> list[dict]:
    """列出所有历史诊断运行。"""
    base = Path(base_dir)
    if not base.exists():
        return []
    runs = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        run_id = d.name
        mf = d / "manifest.json"
        pf = d / "params.json"
        info = {"run_id": run_id}
        if mf.exists():
            with open(mf) as f:
                m = json.load(f)
                info["saved"] = m.get("saved", [])
                info["created_at"] = m.get("created_at", "")
        if pf.exists():
            with open(pf) as f:
                info["params"] = json.load(f)
        # 交易统计
        tf = d / "trades.parquet"
        if tf.exists():
            try:
                tdf = pd.read_parquet(tf)
                info["n_trades"] = len(tdf)
                info["win_rate"] = f"{(tdf['net_return'] > 0).mean():.1%}"
                info["trade_ret_sum"] = f"{tdf['net_return'].sum():+.2%}"
            except Exception:
                pass
        # NAV 统计
        nf = d / "nav.parquet"
        if nf.exists():
            try:
                ndf = pd.read_parquet(nf)
                nav = ndf["nav"]
                info["nav_return"] = f"{float((nav.iloc[-1] / nav.iloc[0] - 1)):+.2%}"
                info["max_dd"] = f"{float((nav / nav.cummax() - 1).min()):.1%}"
            except Exception:
                pass
        runs.append(info)
    return runs


# ═══════════════════════════════════════════
# 查询函数
# ═══════════════════════════════════════════

def query_funnel_summary(data: dict) -> pd.DataFrame:
    """构建入场漏斗汇总：各阶段通过率。"""
    funnel = data.get("entry_funnel")
    if funnel is None or funnel.empty:
        return pd.DataFrame({"阶段": [], "通过数": [], "通过率": []})

    n = len(funnel)
    stages = [
        ("候选池总计", n, 1.0),
        ("未持仓过滤",
         int((~funnel["in_holdings"]).sum()),
         float((~funnel["in_holdings"]).mean())),
        ("非同周锁定",
         int((~funnel["same_week_lock"]).sum()),
         float((~funnel["same_week_lock"]).mean())),
        ("MACD信号就绪",
         int(funnel["has_ms"].sum()),
         float(funnel["has_ms"].mean())),
        ("行情数据存在",
         int(funnel["has_data"].sum()),
         float(funnel["has_data"].mean())),
        ("MA20数据就绪",
         int(funnel["ma20_ready"].sum()),
         float(funnel["ma20_ready"].mean())),
        ("收盘站上MA20",
         int(funnel["ma20_pass"].sum()),
         float(funnel["ma20_pass"].mean())),
    ]
    ma250_ready = int(funnel["ma250_ready"].sum())
    if ma250_ready > 0:
        stages.append(("年线不下行",
                       int(funnel["ma250_pass"].sum()),
                       float(funnel["ma250_pass"].mean())))
    stages += [
        ("板块在强势名单",
         int(funnel["sector_match"].sum()),
         float(funnel["sector_match"].mean())),
        ("MA20>MA60",
         int(funnel["ma_momentum"].sum()),
         float(funnel["ma_momentum"].mean())),
        ("DIF>0",
         int(funnel["dif_gt_0"].sum()),
         float(funnel["dif_gt_0"].mean())),
        ("量能≥MA44",
         int(funnel["vol_ma44_confirm"].sum()),
         float(funnel["vol_ma44_confirm"].mean())),
        ("5条件≥4通过",
         int((funnel["confirm_count"] >= 4).sum()),
         float((funnel["confirm_count"] >= 4).mean())),
        ("最终买入",
         int(funnel["final_pass"].sum()),
         float(funnel["final_pass"].mean())),
    ]
    df = pd.DataFrame(stages, columns=["阶段", "通过数", "通过率"])
    df["通过率"] = df["通过率"].map("{:.1%}".format)
    return df


def query_why_not_traded(data: dict, symbol: str, date: str) -> dict:
    """追踪一只股票在某日未被买入的具体原因。"""
    funnel = data.get("entry_funnel")
    if funnel is None or funnel.empty:
        return {"verdict": "无入场漏斗数据(诊断模式未启用?)", "records": []}

    mask = (funnel["symbol"] == symbol) & (funnel["date"] == date)
    records = funnel[mask]
    if records.empty:
        # 检查这只票是否在 L4 池中
        pool = data.get("weekly_l4_pool")
        if pool is not None:
            in_pool_dates = pool[pool["symbol"] == symbol]["week"].unique()
            if len(in_pool_dates) > 0:
                return {"verdict": f"在候选池中(周: {list(in_pool_dates)})但当日({date})无检查记录",
                        "records": []}
        return {"verdict": "不在当日候选池中", "records": []}

    result = []
    for _, r in records.iterrows():
        reasons = []
        if r["in_holdings"]:
            reasons.append("已持仓")
        if r["same_week_lock"]:
            reasons.append("同周已买入")
        if not r["has_ms"]:
            reasons.append("无MACD信号")
        if not r["has_data"]:
            reasons.append("无当日行情")
        if not r["ma20_ready"]:
            reasons.append("MA20数据不足")
        elif not r["ma20_pass"]:
            reasons.append("未站上MA20")
        if r["ma250_ready"] and not r["ma250_pass"]:
            reasons.append("年线下行")
        if not r["sector_match"]:
            reasons.append("板块不在强势名单")
        if r["confirm_count"] < 2:
            reasons.append(f"入场信号{r['confirm_count']}/4不足")

        result.append({
            "symbol": str(r["symbol"]),
            "sector": str(r.get("sector", "")),
            "confirm_count": int(r["confirm_count"]),
            "signals": {
                "MA20>MA60": bool(r.get("ma_momentum", False)),
                "close>MA20": bool(r.get("ma20_pass", False)),
                "不追高偏离度": round(float(r.get("near_ma20", 0)), 3),
                "DIF>0": bool(r.get("dif_gt_0", False)),
                "量能≥MA44": bool(r.get("vol_ma44_confirm", False)),
            },
            "ma20_pass": bool(r["ma20_pass"]),
            "ma250_pass": bool(r["ma250_pass"]) if r["ma250_ready"] else "N/A",
            "sector_match": bool(r["sector_match"]),
            "passed": bool(r["final_pass"]),
            "blocked_by": reasons,
        })
    verdict = "已买入" if any(r["final_pass"] for _, r in records.iterrows()) else "未通过筛选"
    return {"verdict": verdict, "records": result}


def query_trades(data: dict, symbol: str | None = None) -> pd.DataFrame | None:
    """查询交易明细。"""
    trades = data.get("trades")
    if trades is None or trades.empty:
        return None
    if symbol:
        trades = trades[trades["symbol"] == symbol]
    return trades


def query_market_state(data: dict) -> pd.DataFrame | None:
    """查询市场状态分布。"""
    ms = data.get("market_state")
    if ms is None or ms.empty:
        return None
    counts = ms["state"].value_counts()
    total = counts.sum()
    df = pd.DataFrame({
        "状态": counts.index,
        "天数": counts.values,
        "占比": [f"{c / total:.1%}" for c in counts.values],
    })
    return df


def query_sector_timeline(data: dict, sector_name: str | None = None) -> pd.DataFrame | None:
    """查询板块强势周排名时序。"""
    ws = data.get("weekly_sectors")
    if ws is None or ws.empty:
        return None
    if sector_name:
        ws = ws[ws["sector"] == sector_name]
    return ws.sort_values(["week", "rank"])


def query_pool_timeline(data: dict, symbol: str | None = None) -> pd.DataFrame | None:
    """查询L4池候选股时序。"""
    pool = data.get("weekly_l4_pool")
    if pool is None or pool.empty:
        return None
    if symbol:
        pool = pool[pool["symbol"] == symbol]
    return pool.sort_values(["week", "rank"])


def print_report(data: dict, title: str = "回测诊断报告") -> None:
    """打印完整诊断报告。"""
    width = 72
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")

    # 市场状态分布
    ms = query_market_state(data)
    if ms is not None:
        print(f"\n  市场状态分布:")
        for _, r in ms.iterrows():
            print(f"    {r['状态']:>8}: {r['天数']:>5d} 天 ({r['占比']})")

    # 交易统计
    trades = data.get("trades")
    if trades is not None and not trades.empty:
        print(f"\n  交易统计:")
        print(f"    总交易: {len(trades)} 笔")
        print(f"    胜率: {(trades['net_return'] > 0).mean():.1%}")
        print(f"    总收益: {trades['net_return'].sum():+.2%}")
        # 退出原因分布
        print(f"\n  退出原因分布:")
        for reason, grp in trades.groupby("exit_reason"):
            print(f"    {reason:20s}: {len(grp):>4d} 笔 胜率{(grp['net_return'] > 0).mean():.1%} 均收益{grp['net_return'].mean():+.2%}")

    # 净值
    nav = data.get("nav")
    if nav is not None and not nav.empty:
        nav_vals = nav["nav"]
        ret = float(nav_vals.iloc[-1] / nav_vals.iloc[0] - 1)
        dd = float((nav_vals / nav_vals.cummax() - 1).min())
        print(f"\n  净值: {ret:+.2%}  最大回撤: {dd:.1%}")

    # 漏斗
    funnel_summary = query_funnel_summary(data)
    if not funnel_summary.empty:
        print(f"\n  入场漏斗:")
        print(funnel_summary.to_string(index=False))

    # NAV
    print(f"\n{'=' * width}")
