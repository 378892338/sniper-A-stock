"""真实基本面因子模块 — 取代价量伪基本面"""

import numpy as np
import pandas as pd
import akshare as ak
import time


def fetch_financial_indicators(symbol: str) -> dict:
    """获取单只股票的最新财务指标（ROE/毛利率/净利率/营收增速等）"""
    try:
        df = ak.stock_financial_analysis_indicator(symbol=symbol)
        if df.empty:
            return {}
        latest = df.iloc[0]
        return {
            "roe": float(latest.get("净资产收益率", np.nan) or np.nan),
            "gross_margin": float(latest.get("销售毛利率", np.nan) or np.nan),
            "net_margin": float(latest.get("销售净利率", np.nan) or np.nan),
            "revenue_growth": float(latest.get("营业收入同比增长", np.nan) or np.nan),
            "eps": float(latest.get("每股收益", np.nan) or np.nan),
            "current_ratio": float(latest.get("流动比率", np.nan) or np.nan),
            "debt_ratio": float(latest.get("资产负债率", np.nan) or np.nan),
        }
    except Exception:
        return {}


class FundamentalCache:
    """基本面数据缓存，避免重复请求"""

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def get(self, symbol: str) -> dict:
        if symbol not in self._cache:
            self._cache[symbol] = fetch_financial_indicators(symbol)
            time.sleep(0.15)
        return self._cache[symbol]

    def get_all(self, symbols: list[str], verbose: bool = True) -> dict[str, dict]:
        result = {}
        for i, sym in enumerate(symbols):
            result[sym] = self.get(sym)
            if verbose and (i + 1) % 30 == 0:
                hit = sum(1 for v in result.values() if v)
                print(f"  基本面数据: {i+1}/{len(symbols)} (有效 {hit})")
        return result

    def to_factor_scores(self, symbols: list[str]) -> dict[str, float]:
        """将基本面数据转为因子评分 (0-100)"""
        data = self.get_all(symbols, verbose=False)
        scores = {}
        for sym, d in data.items():
            if not d:
                scores[sym] = 50.0
                continue
            # 综合评分: ROE + 毛利率 + 净利率 + 营收增速
            score = 0
            count = 0
            for key in ["roe", "gross_margin", "net_margin", "revenue_growth"]:
                val = d.get(key, np.nan)
                if not np.isnan(val):
                    score += val
                    count += 1
            scores[sym] = score / count if count > 0 else 50.0
        return scores
