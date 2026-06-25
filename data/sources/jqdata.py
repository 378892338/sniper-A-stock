"""JQData（聚宽）数据源客户端

提供申万行业分类数据获取能力，每进程仅 auth 一次。

使用前需安装: pip install jqdatasdk
账号需开通 SDK 权限（免费试用 3 个月）。
"""

import os
from typing import Any

import pandas as pd

from config.settings import JQDATA_USERNAME, JQDATA_PASSWORD
from core.logger import get_logger
from data.interfaces import DataSource

logger = get_logger("data.sources.jqdata")

# 模块级状态：确保每进程仅 auth 一次
_authenticated: bool = False


def ensure_auth() -> bool:
    """确保 JQData 已认证。每进程仅 auth 一次，返回是否认证成功。

    必须先调用此函数再使用任何 JQData 数据函数。
    auth 状态不跨进程持久化，因此每个使用 JQData 的独立进程都必须重新 auth。
    """
    global _authenticated
    if _authenticated:
        return True

    try:
        import jqdatasdk as jq
    except ImportError:
        logger.debug("jqdatasdk 未安装（跳过 jqdata 源）")
        return False

    username = JQDATA_USERNAME or os.environ.get("JQDATA_USERNAME")
    password = JQDATA_PASSWORD or os.environ.get("JQDATA_PASSWORD")
    if not username or not password:
        logger.debug("JQData 账号未配置（跳过 jqdata 源）")
        return False

    try:
        jq.auth(username, password)
        if jq.is_auth():
            _authenticated = True
            logger.debug("JQData auth 成功")
            return True
        else:
            logger.debug("JQData auth 失败")
            return False
    except Exception as e:
        logger.debug(f"JQData auth 异常: {e}")
        return False


def get_stock_list() -> list[str]:
    """获取全 A 股代码列表（聚宽格式）。

    Returns:
        ['000001.XSHE', '000002.XSHE', ...]
    """
    import jqdatasdk as jq
    stocks = jq.get_all_securities(types=["stock"])
    return stocks.index.tolist()


def get_industry_batch(
    stocks: list[str],
    date: str | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """批量获取个股的行业分类。

    Args:
        stocks: 股票代码列表（聚宽格式，如 '000001.XSHE'）
        date: 查询日期 YYYY-MM-DD。None 表示最新数据。
              免费试用账号数据范围为 2025-03-09 ~ 2026-03-16。

    Returns:
        {
            '000001.XSHE': {
                'sw_l1': {'industry_code': '801780', 'industry_name': '银行I'},
                'sw_l2': {'industry_code': '801783', 'industry_name': '股份制银行II'},
                ...
            },
            ...
        }
    """
    import jqdatasdk as jq
    return jq.get_industry(stocks, date=date)  # type: ignore[no-any-return]


def get_sw_industry_map(
    date: str = "2026-03-16",
) -> pd.DataFrame:
    """获取全 A 股申万行业分类映射。

    使用 get_industry() 一次调用拉取全市场，返回简洁的 DataFrame。

    Args:
        date: 查询日期（必须在试用期 2025-03-09 ~ 2026-03-16 内）

    Returns:
        DataFrame [symbol, industry_l1, industry_l2, industry_l1_code, industry_l2_code]
        - industry_l1: 申万一级行业名（如'银行'，已去除"I/II/III"后缀）
        - industry_l2: 申万二级行业名（如'股份制银行'）
        - industry_l1_code: 申万一级行业代码（如'801780'）
        - industry_l2_code: 申万二级行业代码（如'801783'）
    """
    import jqdatasdk as jq

    stocks = jq.get_all_securities(types=["stock"])
    stock_list = stocks.index.tolist()
    logger.info(f"JQData 获取全市场行业分类: {len(stock_list)} 只...")

    raw = jq.get_industry(stock_list, date=date)
    logger.info(f"JQData 返回: {len(raw)} 只")

    rows: list[dict[str, Any]] = []
    for code, industries in raw.items():
        # 清洗行业名：去掉 "I" / "II" / "III" 后缀及空格
        sw_l1 = industries.get("sw_l1", {})
        sw_l2 = industries.get("sw_l2", {})

        l1_name_raw = (sw_l1 or {}).get("industry_name", "") or ""
        l2_name_raw = (sw_l2 or {}).get("industry_name", "") or ""

        if not l1_name_raw and not l2_name_raw:
            continue

        # 去后缀：' 银行I' → '银行', '股份制银行II' → '股份制银行'
        l1_name = _strip_sw_suffix(l1_name_raw)
        l2_name = _strip_sw_suffix(l2_name_raw)
        l1_code = (sw_l1 or {}).get("industry_code", "") or ""
        l2_code = (sw_l2 or {}).get("industry_code", "") or ""

        rows.append({
            "symbol": code,
            "industry_l1": l1_name,
            "industry_l2": l2_name,
            "industry_l1_code": l1_code,
            "industry_l2_code": l2_code,
        })

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["symbol"])
    df = df.sort_values("symbol").reset_index(drop=True)

    logger.info(
        f"SW 行业分类: {len(df)} 条, "
        f"{df['industry_l1'].nunique()} 一级行业, "
        f"{df['industry_l2'].nunique()} 二级行业"
    )
    return df


def _strip_sw_suffix(name: str) -> str:
    """去除申万行业名后缀 'I' / 'II' / 'III' 及空格。

    '银行I' → '银行'
    '股份制银行II' → '股份制银行'
    '电力设备I' → '电力设备'
    """
    if not name:
        return ""
    # 去掉末尾的 I, II, III（罗马数字，可能有空格在前）
    import re
    name = re.sub(r"\s*[IVX]+\s*$", "", name).strip()
    return name


# ── 行情 DataSource ──

class JQDataSource(DataSource):
    """聚宽数据源 — 日线行情（需 jqdatasdk + 账号）"""

    ENDPOINT = "jqdata_price"

    def __init__(self):
        from shared.anticrawl import AntiCrawlGuard
        self.guard = AntiCrawlGuard("jqdata")

    def name(self) -> str:
        return "jqdata"

    def is_available(self) -> bool:
        return ensure_auth()

    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        self.guard.wait()
        if not ensure_auth():
            return pd.DataFrame()
        import jqdatasdk as jq
        try:
            jq_code = self._to_jq_code(symbol)
            df = jq.get_price(
                jq_code,
                start_date=start, end_date=end,
                frequency="daily",
                fields=["open", "close", "high", "low", "volume", "money"],
                fq="pre",  # 前复权（与 baostock adjustflag=2 一致）
                skip_paused=False,
                panel=False,
                fill_paused=False,
            )
            if df is None or df.empty:
                return pd.DataFrame()
            # 重置 index 并统一列名
            df = df.reset_index()
            # get_price 返回的 index 列名可能是 'index' 或 'date'
            date_col = [c for c in df.columns if c.lower() in ("index", "date")][:1]
            if date_col:
                df = df.rename(columns={date_col[0]: "date"})
            return df
        except Exception as e:
            logger.debug(f"JQData fetch_daily {symbol} 失败: {e}")
            return pd.DataFrame()

    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """聚宽不支持指数日线（通过 akshare 获取），返回空。"""
        return pd.DataFrame()

    @staticmethod
    def _to_jq_code(symbol: str) -> str:
        """6 位代码 → 聚宽格式 (600000 → 600000.XSHG, 000001 → 000001.XSHE)"""
        sym = symbol.strip().zfill(6)
        if sym.startswith(("6", "9")):
            return f"{sym}.XSHG"
        return f"{sym}.XSHE"
