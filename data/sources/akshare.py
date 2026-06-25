"""akshare 数据源实现"""

import time

import pandas as pd

from data.interfaces import DataSource, FundFlowSource
from shared.retry import retry, health_tracker
from shared.anticrawl import AntiCrawlGuard
from core.logger import get_logger

logger = get_logger("data.akshare")


class AkshareDataSource(DataSource):
    """基于 akshare 的行情数据源"""

    ENDPOINT = "akshare_price"

    def __init__(self):
        self.guard = AntiCrawlGuard("akshare")

    def name(self) -> str:
        return "akshare"

    def is_available(self) -> bool:
        return health_tracker.is_available(self.ENDPOINT)

    @retry(max_retries=3, base_delay=1.0)
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak
        self.guard.wait()
        try:
            # 交易所前缀：6xx→sh, 0xx/3xx/4xx→sz, 8xx→bj
            if symbol.startswith(("6", "9")):
                code = f"sh{symbol}"
            elif symbol.startswith(("0", "3", "4")):
                code = f"sz{symbol}"
            else:
                code = f"bj{symbol}"
            start_fmt = start.replace("-", "")
            end_fmt = end.replace("-", "")
            df = ak.stock_zh_a_hist_tx(
                symbol=code,
                start_date=start_fmt,
                end_date=end_fmt,
            )
            # TX 端点列名可能是中文或英文，自适应映射
            _COL_MAP = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交额": "amount",
                "换手率": "turnover", "涨跌幅": "pct_chg",
            }
            existing = {k: v for k, v in _COL_MAP.items() if k in df.columns}
            df = df.rename(columns=existing)
            # TX 端点不返回成交量，补默认值
            if "volume" not in df.columns:
                df["volume"] = 0
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = symbol
            health_tracker.record_success(self.ENDPOINT)
            self.guard.on_success()
            return df.set_index("date")
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            self.guard.on_failure()
            raise e

    @retry(max_retries=3, base_delay=1.0)
    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak
        try:
            # Handle both formats: "sh000001" or "000001"
            if code.startswith("sh") or code.startswith("sz") or code.startswith("bj"):
                symbol = code
            elif code.startswith("000"):
                symbol = f"sh{code}"
            else:
                symbol = f"sz{code}"
            df = ak.stock_zh_index_daily(symbol=symbol)
            if df.empty:
                return pd.DataFrame()
            df = df.rename(columns={
                "date": "date", "open": "open", "close": "close",
                "high": "high", "low": "low", "volume": "volume",
                "amount": "amount",
            })
            df["date"] = pd.to_datetime(df["date"])
            mask = (df["date"] >= start) & (df["date"] <= end)
            df = df[mask]
            df["symbol"] = code
            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date")
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"获取指数 {code} 失败: {e}")
            raise

    def fetch_daily_batch(self, symbols: list[str], start: str, end: str,
                          sleep: float = 0.3) -> dict[str, pd.DataFrame]:
        result = {}
        failed = 0
        for i, sym in enumerate(symbols):
            df = self.fetch_daily(sym, start, end)
            if not df.empty:
                result[sym] = df
            else:
                failed += 1
            if (i + 1) % 50 == 0:
                logger.info(f"批量获取: {i+1}/{len(symbols)} (成功{len(result)}, 失败{failed})")
            time.sleep(sleep)
        logger.info(f"批量获取完成: {len(result)} 成功, {failed} 失败")
        return result

    @retry(max_retries=3, base_delay=1.0)
    def fetch_industry_members(self) -> pd.DataFrame:
        import akshare as ak
        # 获取行业板块列表
        boards = ak.stock_board_industry_name_em()
        # 筛选我们关心的行业（提高效率）
        from data.index_etf import INDUSTRY_TO_ETF
        target_industries = set(INDUSTRY_TO_ETF.keys())
        target_rows = []
        for _, row in boards.iterrows():
            board_name = str(row.iloc[1])
            if board_name in target_industries:
                try:
                    cons = ak.stock_board_industry_cons_em(symbol=board_name)
                    if cons is not None and not cons.empty:
                        col_code = cons.columns[1]  # 代码列
                        for _, c in cons.iterrows():
                            target_rows.append({"symbol": str(c.iloc[1]), "industry": board_name})
                except Exception:
                    continue
        result = pd.DataFrame(target_rows)
        if not result.empty:
            health_tracker.record_success(self.ENDPOINT)
        return result

    @retry(max_retries=3, base_delay=1.0)
    def fetch_concept_members(self) -> pd.DataFrame:
        import akshare as ak
        df = ak.stock_board_concept_cons_em()
        df = df.rename(columns={
            "代码": "symbol", "名称": "name", "板块名称": "concept",
        })
        health_tracker.record_success(self.ENDPOINT)
        return df


# ── 新浪日线数据源（stock_zh_a_daily）──


class AkshareDailySource(DataSource):
    """基于 akshare.stock_zh_a_daily 的日线数据源 — 新浪源，同时提供 volume + amount。

    与 baostock 收盘价一致，前复权。
    性能：~0.5s/只，10/10 成功率（实测 2026-06-23）。
    """

    ENDPOINT = "akshare_daily_price"

    def __init__(self):
        self.guard = AntiCrawlGuard("akshare_daily")

    def name(self) -> str:
        return "akshare_daily"

    def is_available(self) -> bool:
        return health_tracker.is_available(self.ENDPOINT)

    @retry(max_retries=2, base_delay=1.0)
    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak
        self.guard.wait()

        # 加市场前缀: 000001 → sz000001, 600000 → sh600000
        if symbol.startswith(("6", "9")):
            code = f"sh{symbol}"
        else:
            code = f"sz{symbol}"

        try:
            df = ak.stock_zh_a_daily(symbol=code, adjust="qfq")
            if df.empty:
                return pd.DataFrame()

            df["date"] = pd.to_datetime(df["date"])
            mask = (df["date"] >= start) & (df["date"] <= end)
            df = df[mask]
            df = df.dropna(subset=["close"])
            df["symbol"] = symbol

            health_tracker.record_success(self.ENDPOINT)
            self.guard.on_success()
            return df.set_index("date")
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            self.guard.on_failure()
            logger.warning(f"akshare_daily 获取 {symbol} 失败: {e}")
            raise

    @retry(max_retries=2, base_delay=1.0)
    def fetch_index_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        self.guard.wait()
        # ak.stock_zh_a_daily 不支持指数，返回空
        return pd.DataFrame()


class AkshareFundFlowSource(FundFlowSource):
    """基于 akshare 的资金流数据源"""

    ENDPOINT = "akshare_fund_flow"

    def __init__(self):
        self.guard = AntiCrawlGuard("akshare")

    def name(self) -> str:
        return "akshare_fund_flow"

    def is_available(self) -> bool:
        return health_tracker.is_available(self.ENDPOINT)

    @retry(max_retries=2, base_delay=1.0)
    def fetch_northbound_flow(self, start: str, end: str) -> pd.DataFrame | None:
        import akshare as ak
        self.guard.wait()
        try:
            df = ak.stock_hsgt_hist_em(symbol="沪股通")
            if df.empty:
                return None
            # 列名映射: 日期→date, 当日成交净买额→net_flow
            df = df.rename(columns={
                df.columns[0]: "date",
            })
            # 查找净买额列
            for col in df.columns:
                if "净买" in str(col):
                    df = df.rename(columns={col: "net_flow"})
                    break
            df["date"] = pd.to_datetime(df["date"])
            mask = (df["date"] >= start) & (df["date"] <= end)
            df = df[mask]
            health_tracker.record_success(self.ENDPOINT)
            return df.set_index("date") if not df.empty else None
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"获取北向资金失败: {e}")
            return None

    @retry(max_retries=2, base_delay=1.0)
    def fetch_market_turnover(self, code: str, start: str, end: str) -> pd.DataFrame | None:
        """通过指数日线的成交额字段替代"""
        import akshare as ak
        self.guard.wait()
        try:
            if code.startswith("sh") or code.startswith("sz") or code.startswith("bj"):
                symbol = code
            elif code.startswith("000"):
                symbol = f"sh{code}"
            else:
                symbol = f"sz{code}"
            df = ak.stock_zh_index_daily(symbol=symbol)
            df = df.rename(columns={"date": "date", "amount": "amount"})
            if "date" not in df.columns:
                return None
            df["date"] = pd.to_datetime(df["date"])
            mask = (df["date"] >= start) & (df["date"] <= end)
            health_tracker.record_success(self.ENDPOINT)
            return df[mask].set_index("date")
        except Exception as e:
            health_tracker.record_failure(self.ENDPOINT)
            logger.warning(f"获取成交额数据失败: {e}")
            return None
