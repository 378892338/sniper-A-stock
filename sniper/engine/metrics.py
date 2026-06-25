"""绩效评估指标"""

import math
import pandas as pd
import numpy as np

from core.logger import get_logger

logger = get_logger("sniper.engine.metrics")


def calculate_metrics(daily_values: list[dict], trades: list[dict],
                      initial_capital: float, risk_free: float = 0.02) -> dict:
    """计算完整绩效指标。"""
    if not daily_values:
        return {}

    df = pd.DataFrame(daily_values)
    if df.empty or "total_value" not in df.columns:
        return {}

    # 日收益率序列
    df["return"] = df["total_value"].pct_change().fillna(0.0)
    returns = df["return"].values

    # 基础指标
    total_return = (df["total_value"].iloc[-1] - initial_capital) / initial_capital
    n_days = len(df)
    n_years = n_days / 252
    annual_return = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else 0.0

    # 波动率
    daily_vol = np.std(returns, ddof=1)
    annual_vol = daily_vol * math.sqrt(252)

    # Sharpe
    excess = annual_return - risk_free
    sharpe = excess / annual_vol if annual_vol > 0 else 0.0

    # 最大回撤
    peak = np.maximum.accumulate(df["total_value"].values)
    drawdowns = (df["total_value"].values - peak) / peak
    max_drawdown = abs(np.min(drawdowns))

    # Calmar
    calmar = annual_return / max_drawdown if max_drawdown > 0 else 0.0

    # 交易统计
    buy_trades = [t for t in trades if t.get("action") == "SELL" and "pnl" in t]
    win_trades = [t for t in buy_trades if t.get("pnl", 0) > 0]
    win_rate = len(win_trades) / len(buy_trades) if buy_trades else 0.0
    total_pnl = sum(t.get("pnl", 0) for t in buy_trades)
    avg_pnl = total_pnl / len(buy_trades) if buy_trades else 0.0
    avg_win = sum(t["pnl"] for t in win_trades) / len(win_trades) if win_trades else 0.0
    avg_loss = sum(t["pnl"] for t in buy_trades if t.get("pnl", 0) <= 0) / max(len(buy_trades) - len(win_trades), 1)
    profit_factor = abs(avg_win * len(win_trades) / (avg_loss * (len(buy_trades) - len(win_trades)) + 1))

    metrics = {
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "daily_vol": round(daily_vol, 4),
        "annual_vol": round(annual_vol, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_drawdown, 4),
        "calmar": round(calmar, 3),
        "win_rate": round(win_rate, 4),
        "total_trades": len(buy_trades),
        "avg_pnl": round(avg_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 3),
        "final_capital": round(df["total_value"].iloc[-1], 2),
    }

    logger.info(f"绩效: 年化={metrics['annual_return']:.2%} "
                f"Sharpe={metrics['sharpe']:.2f} "
                f"最大回撤={metrics['max_drawdown']:.2%} "
                f"胜率={metrics['win_rate']:.2%}")
    return metrics


def print_metrics_table(metrics: dict):
    """格式化输出绩效指标。"""
    if not metrics:
        print("无回测结果")
        return
    print("=" * 50)
    print(f"{'绩效指标':^48}")
    print("=" * 50)
    for k, v in metrics.items():
        if isinstance(v, float):
            if "rate" in k or "return" in k or "drawdown" in k:
                print(f"  {k:20s}: {v:.2%}")
            else:
                print(f"  {k:20s}: {v:.4f}")
        else:
            print(f"  {k:20s}: {v}")
    print("=" * 50)
