"""已推送股票每日追踪 — 退出信号检测 + 停牌/复牌处理"""

import pandas as pd

from core.logger import get_logger

logger = get_logger("engine.watcher")


def check_all_exit_signals(
    symbol: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame = None,
    sector_strength: dict[str, bool] = None,
    fund_data: dict = None,
    etf_tags: list[str] = None,
    etf_strong_pool: set[str] = None,
    l1_is_strong: bool = None,
) -> dict:
    """
    检查所有退出信号。

    退出信号优先级: 个股退出 > 指数退出 > 日频预警冻结

    返回: {triggered: bool, signals: [str], action: str}
    """
    from gate.layer3_stock import check_exit_signals

    result = check_exit_signals(
        symbol, daily_df, weekly_df,
        fund_data=fund_data,
        etf_tags=etf_tags,
        etf_strong_pool=etf_strong_pool,
        l1_is_strong=l1_is_strong,
    )

    return result


def daily_track_positions(
    holding_stocks: dict[str, dict],  # {symbol: {daily_df, weekly_df, sector, etf_tags}}
    sector_strength: dict[str, bool] = None,
    fund_data: dict[str, dict] | None = None,
    etf_strong_pool: set[str] = None,
    l1_is_strong: bool = None,
) -> list[dict]:
    """
    每日追踪已持仓股票。

    退出信号:
    1. 周线MACD死叉 → 即时推送卖出
    2. 日线出现顶背驰 → 即时推送卖出
    3. 资金面连续3日净流出 → 即时推送卖出
    4. 所属分类指数跌出强势池 → 即时推送卖出
    5. 第一层大盘环境转弱 → 即时推送全部清仓
    6. 仍然满足 → 持仓，不重复推送

    优先级: 个股退出信号 > 指数退出信号 > 日频预警冻结

    返回: [{symbol, action, reason}]
    """
    actions = []

    for symbol, data in holding_stocks.items():
        daily = data.get("daily_df")
        weekly = data.get("weekly_df")

        if daily is None or daily.empty:
            continue

        symbol_fund = fund_data.get(symbol) if fund_data else None
        symbol_etf_tags = data.get("etf_tags")
        exit_result = check_all_exit_signals(
            symbol, daily, weekly, sector_strength=sector_strength,
            fund_data=symbol_fund,
            etf_tags=symbol_etf_tags,
            etf_strong_pool=etf_strong_pool,
            l1_is_strong=l1_is_strong,
        )

        if exit_result["triggered"]:
            actions.append({
                "symbol": symbol,
                "action": exit_result["action"],
                "reason": "; ".join(exit_result["signals"]),
                "time": pd.Timestamp.now(),
            })
            logger.info(f"退出信号: {symbol} — {exit_result['signals']}")

    return actions


# 复牌追踪: {symbol: 首次发现日期}
_resume_tracker: dict[str, pd.Timestamp] = {}


def handle_stock_resumed(symbol: str, daily_df: pd.DataFrame,
                         weekly_df: pd.DataFrame = None) -> dict:
    """
    停牌股复牌处理。

    复牌首日: 不操作，观察一天
    复牌次日: 重新评估
    先检查周线MACD和日线背驰
    - 触发任一 → 直接卖出
    - 未触发 → 进入正常次日评估
    """
    global _resume_tracker
    from factors.macd import calc_macd, is_death_cross
    from factors.chanlun.divergence import check_daily_top_divergence

    daily_dif, _, daily_hist = calc_macd(daily_df["close"])
    latest_date = daily_df.index[-1]

    # 检查周线MACD死叉
    if weekly_df is not None and not weekly_df.empty:
        w_dif, w_dea, _ = calc_macd(weekly_df["close"])
        if bool(is_death_cross(w_dif, w_dea).tail(2).any()):
            _resume_tracker = {k: v for k, v in _resume_tracker.items() if k != symbol}
            return {"symbol": symbol, "action": "卖出", "reason": "复牌后周线MACD死叉"}

    # 检查日线背驰
    if check_daily_top_divergence(daily_df, daily_hist):
        _resume_tracker = {k: v for k, v in _resume_tracker.items() if k != symbol}
        return {"symbol": symbol, "action": "卖出", "reason": "复牌后日线顶背驰"}

    if symbol not in _resume_tracker:
        _resume_tracker = {**_resume_tracker, symbol: latest_date}
        return {"symbol": symbol, "action": "观察", "reason": "复牌首日，次日评估"}

    if latest_date > _resume_tracker[symbol]:
        _resume_tracker = {k: v for k, v in _resume_tracker.items() if k != symbol}
        return {"symbol": symbol, "action": "评估", "reason": "复牌次日，重新评估"}

    return {"symbol": symbol, "action": "观察", "reason": "复牌首日，次日评估"}
