"""回测引擎"""

import numpy as np
import pandas as pd
from pandas.tseries.offsets import BDay

from config.settings import BACKTEST_START, BACKTEST_END, REBALANCE_FREQ, BENCHMARK


def generate_rebalance_dates(start: str, end: str, freq: str = "monthly") -> list[str]:
    """生成调仓日期列表"""
    if freq == "monthly":
        dates = pd.date_range(start, end, freq="BME")  # 月末交易日
    elif freq == "weekly":
        dates = pd.date_range(start, end, freq="W-FRI")
    else:
        dates = pd.date_range(start, end, freq="B")
    return [d.strftime("%Y-%m-%d") for d in dates]


def calc_metrics(returns: pd.Series, benchmark_returns: pd.Series = None, rf: float = 0.02) -> dict:
    """计算回测指标"""
    er = returns.dropna()
    if er.empty:
        return {}

    total_days = len(er)
    years = total_days / 252

    total_ret = (1 + er).prod() - 1
    annual_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0
    annual_vol = er.std() * np.sqrt(252)
    sharpe = (annual_ret - rf) / annual_vol if annual_vol > 0 else 0

    cummax = (1 + er).cumprod().cummax()
    drawdown = (1 + er).cumprod() / cummax - 1
    max_dd = drawdown.min()

    win_rate = (er > 0).mean()

    # Alpha / Beta
    alpha, beta = np.nan, np.nan
    if benchmark_returns is not None and not benchmark_returns.empty:
        br = benchmark_returns.reindex(er.index).dropna()
        common_idx = er.index.intersection(br.index)
        if len(common_idx) > 60:
            er_c = er.loc[common_idx]
            br_c = br.loc[common_idx]
            beta = np.cov(er_c.values, br_c.values)[0, 1] / np.var(br_c.values)
            alpha = annual_ret - (rf + beta * (br_c.mean() * 252 - rf))

    return {
        "total_return": round(total_ret * 100, 2),
        "annual_return": round(annual_ret * 100, 2),
        "annual_volatility": round(annual_vol * 100, 2),
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown": round(max_dd * 100, 2),
        "win_rate": round(win_rate * 100, 2),
        "alpha": round(alpha, 4) if not np.isnan(alpha) else None,
        "beta": round(beta, 4) if not np.isnan(beta) else None,
    }


# 交易成本参数 (A股: 佣金万2.5双边 + 印花税千1卖出 + 滑点万1)
COMMISSION_RATE = 0.00025   # 万2.5 佣金
STAMP_TAX_RATE = 0.001      # 千1 印花税(仅卖出)
SLIPPAGE_RATE = 0.0001      # 万1 滑点
TRANSACTION_COST = COMMISSION_RATE * 2 + STAMP_TAX_RATE + SLIPPAGE_RATE  # 约千1.5


class BacktestEngine:
    """回测引擎"""

    def __init__(
        self,
        start: str = BACKTEST_START,
        end: str = BACKTEST_END,
        freq: str = REBALANCE_FREQ,
        top_n: int = 30,
        transaction_cost: float = TRANSACTION_COST,
    ):
        self.start = start
        self.end = end
        self.freq = freq
        self.top_n = top_n
        self.benchmark = BENCHMARK
        self.transaction_cost = transaction_cost

    def run(
        self,
        daily_data: dict[str, pd.DataFrame],
        weekly_data: dict[str, pd.DataFrame] = None,
        strategy: object = None,
    ) -> dict:
        """执行回测"""
        from strategies.sieve import SieveStrategy

        if strategy is None:
            strategy = SieveStrategy()

        rebalance_dates = generate_rebalance_dates(self.start, self.end, self.freq)

        # 计算每日收益率
        daily_returns = {}
        for sym, df in daily_data.items():
            ret = df["close"].pct_change()
            daily_returns[sym] = ret

        # 调仓记录
        holdings_log = []
        portfolio_returns = pd.Series(np.nan, index=pd.date_range(self.start, self.end, freq="B"))

        strategy.init_sectors(self.start, self.end)

        prev_holdings = []

        for i, rd in enumerate(rebalance_dates):
            # 对每只股票打分（使用 score_with_sector 纳入 L0 分类指数加权）
            scores = {}
            for sym, df in daily_data.items():
                if pd.Timestamp(rd) in df.index:
                    wdf = weekly_data.get(sym) if weekly_data else None
                    result = strategy.score_with_sector(
                        sym, df.loc[:rd], rd, wdf
                    )
                    scores[sym] = pd.Series(result["final_score"], index=[rd])

            # 选Top N
            holdings = strategy.select_top(scores, rd, self.top_n)

            # C3: 持仓不足5只时，有prev则沿用，否则保留现有选股避免空列表永久化
            if len(holdings) < 5:
                if prev_holdings:
                    holdings = prev_holdings

            # 计算换手率（H6: 必须用旧的 prev_holdings，在更新 prev_holdings 之前）
            if prev_holdings:
                sold = set(prev_holdings) - set(holdings)
                bought = set(holdings) - set(prev_holdings)
                turnover = (len(sold) + len(bought)) / len(prev_holdings) / 2
            else:
                turnover = 1.0  # 首次建仓 100% 换手
            turnover_cost = turnover * self.transaction_cost

            # H6: 换手率计算之后才更新 prev_holdings
            prev_holdings = holdings

            # 等权
            w = 1.0 / len(holdings) if holdings else 0
            weights = {h: w for h in holdings}

            # 028: 用 enumerate 索引代替 O(n^2) .index() 搜索
            next_date = rebalance_dates[i + 1] if i + 1 < len(rebalance_dates) else self.end

            start_hold = pd.Timestamp(rd) + BDay(1)
            cost_applied = False
            for date in pd.date_range(start_hold, next_date, freq="B"):
                ds = date.strftime("%Y-%m-%d")
                rets = []
                wts = []
                for sym, wt in weights.items():
                    if sym in daily_returns and ds in daily_returns[sym].index:
                        rets.append(daily_returns[sym].loc[ds])
                        wts.append(wt)
                if rets:
                    wts_arr = np.array(wts)
                    wts_arr = wts_arr / wts_arr.sum()  # 025: 归一化权重
                    day_ret = np.sum(np.array(rets) * wts_arr)
                    if not cost_applied:
                        day_ret -= turnover_cost
                        cost_applied = True
                    portfolio_returns.loc[date] = day_ret

            holdings_log.append({
                "date": rd,
                "holdings": holdings,
                "n_stocks": len(holdings),
            })

        # 计算指标
        metrics = calc_metrics(portfolio_returns)
        metrics["rebalance_count"] = len(rebalance_dates)
        metrics["avg_holdings"] = np.mean([h["n_stocks"] for h in holdings_log])

        return {
            "metrics": metrics,
            "portfolio_returns": portfolio_returns,
            "holdings_log": holdings_log,
        }

    def print_report(self, result: dict):
        """打印回测报告"""
        m = result["metrics"]
        print("=" * 50)
        print("回测报告")
        print("=" * 50)
        print(f"回测区间: {self.start} ~ {self.end}")
        print(f"调仓频率: {self.freq}")
        print(f"持仓数量: Top {self.top_n}")
        print(f"累计收益: {m['total_return']}%")
        print(f"年化收益: {m['annual_return']}%")
        print(f"年化波动: {m['annual_volatility']}%")
        print(f"夏普比率: {m['sharpe_ratio']}")
        print(f"最大回撤: {m['max_drawdown']}%")
        print(f"胜率: {m['win_rate']}%")
        if m.get("alpha") is not None:
            print(f"Alpha: {m['alpha']}")
            print(f"Beta: {m['beta']}")
        print(f"调仓次数: {m['rebalance_count']}")
        print(f"平均持仓: {m['avg_holdings']:.0f} 只")
        print("=" * 50)
