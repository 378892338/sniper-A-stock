"""腾讯 qt.gtimg.cn 全市场估值抓取 + 解析。

接口: https://qt.gtimg.cn/q=sh600519,sz000001,...
返回 GBK 编码文本，`~` 分隔字段。
单批最多 50 个 code，批间 0.3s 延时，2 次重试，总失败超 10% 时跳过。
"""

import re
import time
from typing import Any
from urllib.request import urlopen, Request

import pandas as pd

from core.logger import get_logger

logger = get_logger("data.sources.tencent_valuation")

BATCH_SIZE = 50
DELAY = 0.3
RETRIES = 2
FAILURE_THRESHOLD = 0.10  # 总失败率超此值 → 跳过

_URL_TEMPLATE = "https://qt.gtimg.cn/q={}"

# 股票代码前缀映射
_PREFIX_MAP: dict[str, str] = {
    "6": "sh",
    "5": "sh",  # 510xxx ETF 在上海
    "0": "sz",
    "3": "sz",
    "4": "sz",  # 430xxx, 4xxxxx 北交所/三板
    "8": "sz",  # 8xxxxx 北交所
}

# 字段索引（该接口固定格式，~ 分隔）
IDX_NAME = 1       # 股票简称
IDX_PE_TTM = 39    # 滚动市盈率
IDX_TURNOVER = 38  # 换手率 %
IDX_TOTAL_MV = 45  # 总市值（亿元）
IDX_FLOAT_MV = 44  # 流通市值（亿元）
IDX_PB = 46        # 市净率

# 字段数最小要求
_MIN_FIELDS = 47


def _code_to_tencent(symbol: str) -> str:
    """将 600519 转换为 sh600519。"""
    prefix = _PREFIX_MAP.get(symbol[0])
    if prefix is None:
        raise ValueError(f"无法识别股票代码前缀: {symbol}")
    return f"{prefix}{symbol}"


def _parse_response(text: str) -> list[dict[str, Any]]:
    """解析腾讯接口返回文本，返回 dict 列表。"""
    rows: list[dict[str, Any]] = []
    for m in re.finditer(r'v_(\w+)="(.*?)"', text):
        key = m.group(1)  # 如 sh600519
        symbol = _parse_symbol_from_key(key)
        if not symbol:
            continue

        raw = m.group(2)
        fields = raw.split("~")
        if len(fields) < _MIN_FIELDS:
            continue

        def _safe_float(idx: int) -> float | None:
            val = fields[idx].strip() if idx < len(fields) else ""
            if not val or val == "-":
                return None
            try:
                return float(val)
            except ValueError:
                return None

        rows.append({
            "symbol": symbol,
            "name": fields[IDX_NAME].strip() if IDX_NAME < len(fields) else "",
            "pe_ttm": _safe_float(IDX_PE_TTM),
            "pb": _safe_float(IDX_PB),
            "total_mv": _safe_float(IDX_TOTAL_MV),
            "float_mv": _safe_float(IDX_FLOAT_MV),
            "turnover": _safe_float(IDX_TURNOVER),
        })
    return rows


def _parse_symbol_from_key(key: str) -> str | None:
    """从 regex group key 解析股票代码。

    接口返回格式: v_sh600519="...", v_sz000001="..."
    key 为 sh600519 或 sz000001。
    """
    if len(key) < 8:
        return None
    digits = key[2:]  # 去掉 sh/sz 前缀
    if digits.isdigit() and len(digits) == 6:
        return digits
    return None


def fetch_tencent_quotes(codes: list[str]) -> list[dict[str, Any]]:
    """抓取腾讯实时行情。

    Args:
        codes: 股票代码列表，如 ["600519", "000001"]

    Returns:
        dict 列表，每个包含 symbol, name, pe_ttm, pb, total_mv, float_mv, turnover。
        失败项以 None 填充字段，不会跳过。
    """
    tencents = [_code_to_tencent(c) for c in codes]
    batches = [tencents[i:i + BATCH_SIZE] for i in range(0, len(tencents), BATCH_SIZE)]

    all_rows: list[dict[str, Any]] = []
    failures = 0

    for idx, batch in enumerate(batches):
        url = _URL_TEMPLATE.format(",".join(batch))
        data: list[dict[str, Any]] | None = None

        for attempt in range(RETRIES + 1):
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urlopen(req, timeout=10)
                text = resp.read().decode("GBK")
                data = _parse_response(text)
                if data:
                    break
            except Exception as e:
                logger.warning(
                    f"腾讯估值 批次 {idx + 1}/{len(batches)} 失败"
                    f"（尝试 {attempt + 1}/{RETRIES + 1}）: {e}"
                )
                if attempt < RETRIES:
                    time.sleep(DELAY * 2)

        if data is None:
            failures += len(batch)
            for c in batch:
                all_rows.append({
                    "symbol": c,
                    "name": None,
                    "pe_ttm": None,
                    "pb": None,
                    "total_mv": None,
                    "float_mv": None,
                    "turnover": None,
                })
        else:
            all_rows.extend(data)

        if idx < len(batches) - 1:
            time.sleep(DELAY)

    total = len(all_rows)
    failure_rate = failures / total if total > 0 else 1.0
    if failure_rate > FAILURE_THRESHOLD:
        logger.error(
            f"腾讯估值 失败率 {failure_rate:.1%} 超过阈值 {FAILURE_THRESHOLD:.0%}，跳过"
        )
        return []

    logger.info(
        f"腾讯估值 完成: {total} 条, 失败 {failures}/{len(codes)}"
    )
    return all_rows


def fetch_all_valuation() -> pd.DataFrame:
    """从 stock_list 获取全市场代码，拉取估值数据。

    Returns:
        DataFrame, 列为 symbol, name, pe_ttm, pb, total_mv, float_mv, turnover。
        失败时返回空 DataFrame。
    """
    from data.local.warehouse import LocalDataWarehouse
    wh = LocalDataWarehouse()
    stocks = wh.get_stock_list()
    if stocks.empty:
        logger.error("stock_list 为空，无法获取估值数据")
        return pd.DataFrame()

    codes = sorted(stocks["symbol"].tolist())
    rows = fetch_tencent_quotes(codes)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    logger.info(
        f"全市场估值: {len(df)} 行, "
        f"PE 有效 {df['pe_ttm'].notna().sum()}, "
        f"PB 有效 {df['pb'].notna().sum()}, "
        f"市值有效 {df['total_mv'].notna().sum()}"
    )
    return df
