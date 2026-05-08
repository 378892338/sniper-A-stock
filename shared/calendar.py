"""交易日历"""

from datetime import date, timedelta

import pandas as pd

from core.logger import get_logger

logger = get_logger("shared.calendar")

# 中国节假日（2024-2026，非周末休市日）
_CHINESE_HOLIDAYS = {
    # 2024
    date(2024, 1, 1), date(2024, 2, 12), date(2024, 2, 13), date(2024, 2, 14),
    date(2024, 2, 15), date(2024, 2, 16), date(2024, 4, 4), date(2024, 4, 5),
    date(2024, 5, 1), date(2024, 5, 2), date(2024, 5, 3), date(2024, 6, 10),
    date(2024, 9, 16), date(2024, 9, 17), date(2024, 10, 1), date(2024, 10, 2),
    date(2024, 10, 3), date(2024, 10, 4), date(2024, 10, 7),
    # 2025
    date(2025, 1, 1), date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 1, 31), date(2025, 2, 3), date(2025, 2, 4), date(2025, 4, 4),
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 5), date(2025, 6, 2),
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3), date(2025, 10, 6),
    date(2025, 10, 7), date(2025, 10, 8),
    # 2026
    date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 4, 6), date(2026, 5, 1),
    date(2026, 5, 4), date(2026, 5, 5), date(2026, 6, 22), date(2026, 9, 25),
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6),
    date(2026, 10, 7),
}


def generate_trading_calendar(start: str, end: str) -> pd.DatetimeIndex:
    """生成交易日历（含中国节假日过滤）"""
    bdays = pd.bdate_range(start=start, end=end, freq="C", weekmask="Mon Tue Wed Thu Fri")
    return bdays[~bdays.isin(pd.to_datetime(list(_CHINESE_HOLIDAYS)))]


def next_trading_day(dt: pd.Timestamp) -> pd.Timestamp:
    """下一个交易日"""
    next_day = dt + pd.Timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += pd.Timedelta(days=1)
    return next_day


def prev_trading_day(dt: pd.Timestamp) -> pd.Timestamp:
    """上一个交易日"""
    prev_day = dt - pd.Timedelta(days=1)
    while prev_day.weekday() >= 5:
        prev_day -= pd.Timedelta(days=1)
    return prev_day


def is_trading_day(dt: pd.Timestamp) -> bool:
    return dt.weekday() < 5 and dt.date() not in _CHINESE_HOLIDAYS


def week_last_trading_day(dt: pd.Timestamp) -> pd.Timestamp:
    """给定日期所在周最后一个交易日（周五）"""
    days_until_friday = 4 - dt.weekday()
    if days_until_friday < 0:
        days_until_friday += 7
    return dt + pd.Timedelta(days=days_until_friday)


def weeks_between(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """返回 start 到 end 之间每个周的最后一个交易日"""
    result = []
    current = week_last_trading_day(start)
    end_friday = week_last_trading_day(end)
    while current <= end_friday:
        if current >= start:
            result.append(current)
        current += pd.Timedelta(days=7)
    return result


def get_weekly_dates(daily_idx: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """从日线索引中提取每周最后一个交易日"""
    if len(daily_idx) == 0:
        return []
    df = pd.DataFrame({"date": daily_idx})
    df["week"] = daily_idx.isocalendar().week
    df["year"] = daily_idx.isocalendar().year
    return df.groupby(["year", "week"])["date"].max().sort_values().tolist()
