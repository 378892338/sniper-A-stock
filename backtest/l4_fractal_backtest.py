"""L4 "狙击手" Trade-by-Trade 回测 — 5票版（回撤<5%）

架构（五层）:
  L0: 市场状态择时（沪深300趋势+量能+宽度三维评分）
      → "上涨"：正常交易（≤5只）/ "震荡"：轻仓（≤3只）/ "下跌"：完全空仓
  L1: 三维板块评分（动量40%+资金35%+广度25%）
      → 前20%板块入选（约5个），每周轮换
  L2: 8因子个股评分（相对强度+尾盘倾向+量价背离+波动率收缩+
      换手率异常+大单验证+短期反转+均线多头）
      → 每板块Top5选股，总计≤5只
  L3: MA250不下行（硬性）+ 五条件至少4/5通过
      (MA20>MA60 + close>MA20 + 不追高<5% + DIF>0 + vol≥MA44)
  L4: 三层退出（L0转弱 + 硬止损-5% + 前日最低价移动止损）
      + 组合熔断（NAV回撤3.5%停5天）

成本: 买入0.75% | 卖出0.835%
"""

from pathlib import Path
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from tqdm import tqdm

from factors.macd import calc_macd
from core.logger import get_logger
from data.industry import SW_INDEX_MAP

logger = get_logger("backtest.l4_fractal")

BUY_COST = 0.00750
SELL_COST = 0.00835

# ═══════════════════════════════════════════
# L0: 市场状态择时参数
# ═══════════════════════════════════════════
MARKET_STATE_THRESHOLD = 30    # 总分>30=上涨, 20-30=震荡, <20=下跌

# ═══════════════════════════════════════════
# L1: 板块评分参数
# ═══════════════════════════════════════════
SECTOR_MOMENTUM_LOOKBACK = 6       # 板块动量回溯周数
SECTOR_TOP_PERCENTILE = 0.20       # 板块入选前20%（约5个板块）
SECTOR_MOMENTUM_WEIGHT = 0.40
SECTOR_CAPITAL_WEIGHT = 0.35
SECTOR_BREADTH_WEIGHT = 0.25

# ═══════════════════════════════════════════
# L2: 个股因子评分参数
# ═══════════════════════════════════════════
RSR_LOOKBACK_WEEKS = 4              # 相对强度回溯周数
STOCK_RSR_THRESHOLD = 0.8           # 板块内相对强度门槛
STOCKS_PER_SECTOR = 5               # 每板块最多选股数
MAX_HOLDINGS = 5                    # 最大持仓数
MAX_HOLDINGS_VOLATILE = 3           # 震荡市最大持仓数

# L3 入场参数
L3_CONFIRM_MIN = 4                  # 5条件中至少满足4个
MA20_DEVIATION_LIMIT = 0.05         # 不追高：价格离MA20不超过5%

# L3 8因子权重（与 screen_stocks_v2 共享）
L3_RSR_WEIGHT = 0.20
L3_TAIL_WEIGHT = 0.10
L3_VOL_PRICE_WEIGHT = 0.10
L3_VOLATILITY_WEIGHT = 0.15
L3_TURNOVER_WEIGHT = 0.10
L3_BIG_ORDER_WEIGHT = 0.15
L3_REVERSAL_WEIGHT = 0.10
L3_MA_WEIGHT = 0.10

# L4 风控参数
MAX_LOSS_PER_TRADE = 0.05           # 单笔最大亏损-5%
MAX_LOSS_PER_DAY = 0.03             # 当日最大亏损3%
TRAILING_STOP_LOOKBACK = 1          # 移动止损：前N日最低价
PORTFOLIO_DRAWDOWN_LIMIT = 0.035    # 组合熔断：净值回撤3.5%
PORTFOLIO_COOLDOWN_DAYS = 5         # 熔断后停5个交易日
ATR_PERIOD = 14                     # ATR计算周期（诊断用途）

# 旧参数保留过渡
BAOSTOCK_DELAY = 0.5


# ═══════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════


def safe_scalar(val):
    """从可能含重复索引的 .loc 返回值中提取标量。"""
    if isinstance(val, pd.Series):
        val = val.iloc[0]
    return val


# ═══════════════════════════════════════════
# L0: 市场状态择时
# ═══════════════════════════════════════════

def precompute_market_state(store, biz_days: pd.DatetimeIndex) -> pd.Series:
    """预计算每日市场状态（基于沪深300）。

    三维评分:
      趋势(50%): MA位置+方向 → 0-50分
      量能(30%): 成交额 vs MA20 → -15~15分
      宽度(20%): 上涨家数占比 → -10~10分

    总分 >40 → "上涨" / 20-40 → "震荡" / <20 → "下跌"

    返回: pd.Series(index=biz_days, values="bull"/"volatile"/"bear")
    """
    # 获取沪深300日线
    bm = store.get_daily("csi300")
    if bm is None or bm.empty:
        logger.warning("沪深300数据不可用，默认返回震荡")
        return pd.Series("volatile", index=biz_days)

    close = bm["close"]
    volume = bm.get("volume", pd.Series(1e8, index=close.index))

    # 用上证指数成交额代理全市场量能
    sh = store.get_daily("shanghai")
    sh_volume = sh["volume"] if sh is not None and "volume" in sh.columns else pd.Series(float("nan"), index=close.index)

    # 周线用于趋势判断（降低噪音）
    weekly_close = close.resample("W-FRI", closed="right", label="right").last().dropna()
    ma20 = weekly_close.rolling(20, min_periods=1).mean()
    ma60 = weekly_close.rolling(60, min_periods=1).mean()
    ma250 = weekly_close.rolling(250, min_periods=250).mean()

    # 量能MA20
    vol_ma20 = sh_volume.rolling(20, min_periods=1).mean()

    state = pd.Series("volatile", index=biz_days, dtype=object)

    for d in biz_days:
        score = 0.0
        ds = str(d)[:10]
        if ds not in close.index:
            continue

        # ── 趋势得分 (0-50) ──
        # 检查最近周线数据
        recent_weeks = weekly_close.index[weekly_close.index <= d]
        if len(recent_weeks) >= 2:
            last_w = recent_weeks[-1]
            # 当日收盘价（确保标量）
            close_today = close.loc[ds]
            close_today = float(close_today.iloc[0]) if isinstance(close_today, pd.Series) else float(close_today)
            # 在MA250之上 → +30
            if last_w in ma250.index:
                ma250_v = ma250.loc[last_w]
                ma250_v = float(ma250_v.iloc[0]) if isinstance(ma250_v, pd.Series) else float(ma250_v)
                if not pd.isna(ma250_v) and close_today > ma250_v:
                    score += 30
            # MA20>MA60且MA60向上 → +20
            if last_w in ma20.index and last_w in ma60.index:
                ma20_v = float(ma20.loc[last_w].iloc[0]) if isinstance(ma20.loc[last_w], pd.Series) else float(ma20.loc[last_w])
                ma60_v = float(ma60.loc[last_w].iloc[0]) if isinstance(ma60.loc[last_w], pd.Series) else float(ma60.loc[last_w])
                if not (pd.isna(ma20_v) or pd.isna(ma60_v)):
                    prev_60_idx = max(0, _safe_get_loc(weekly_close.index, last_w) - 1)
                    if ma20_v > ma60_v:
                        if prev_60_idx < len(ma60):
                            prev_60 = ma60.iloc[prev_60_idx]
                            prev_60 = float(prev_60.iloc[0]) if isinstance(prev_60, pd.Series) else float(prev_60)
                            if not pd.isna(prev_60) and ma60_v > prev_60:
                                score += 20  # MA20>MA60且MA60向上
                            else:
                                score += 10  # MA20>MA60但MA60走平
                        else:
                            score += 10

        # ── 量能得分 (-15~15) ──
        if ds in sh_volume.index and ds in vol_ma20.index:
            v = sh_volume.loc[ds]
            v = float(v.iloc[0]) if isinstance(v, pd.Series) else float(v)
            vma = vol_ma20.loc[ds]
            vma = float(vma.iloc[0]) if isinstance(vma, pd.Series) else float(vma)
            if not (pd.isna(v) or pd.isna(vma)) and vma > 0:
                if v > vma * 1.2:
                    score += 15
                elif v > vma:
                    pass  # 0分
                else:
                    score -= 15
        else:
            score += 5  # 数据不可用时给中性偏正

        # ── 宽度得分 (-10~10) ──
        # 利用现有个股数据估算涨跌比
        up_ratio = _estimate_market_breadth(store, d)
        if up_ratio > 0.6:
            score += 10
        elif up_ratio > 0.4:
            score += 5
        elif up_ratio > 0:
            score -= 5
        else:
            score -= 10

        # ── 判定 ──
        if score > MARKET_STATE_THRESHOLD:
            state.loc[d] = "bull"
        elif score > 20:
            state.loc[d] = "volatile"
        else:
            state.loc[d] = "bear"

    n_bull = (state == "bull").sum()
    n_vol = (state == "volatile").sum()
    n_bear = (state == "bear").sum()
    logger.info(f"市场状态分布: bull={n_bull}天({n_bull/len(biz_days)*100:.0f}%), "
                f"volatile={n_vol}天({n_vol/len(biz_days)*100:.0f}%), "
                f"bear={n_bear}天({n_bear/len(biz_days)*100:.0f}%)")
    return state


def _estimate_market_breadth(store, as_of: pd.Timestamp) -> float:
    """估算全市场上涨家数占比。

    用 store 中已有的个股数据抽样估算。
    若数据不足，返回 0.5（中性）。

    确定性抽样（按符号排序后取前200只），保证回测可复现。
    """
    stock_names = sorted(getattr(store, "stock_names", []))
    if not stock_names:
        return 0.5
    # 排序后取前200只（确定性，可复现）
    sample = stock_names[:200]
    up_count = 0
    total = 0
    for sym in sample:
        df = store.get_daily(sym)
        if df is None:
            continue
        ds = str(as_of)[:10]
        if ds not in df.index:
            continue
        idx_pos = _safe_get_loc(df.index, ds)
        prev_idx = idx_pos - 1
        if prev_idx < 0:
            continue
        prev_close = float(df.iloc[prev_idx]["close"])
        cur_close = float(df.iloc[idx_pos]["close"])
        if prev_close > 0:
            total += 1
            if cur_close > prev_close:
                up_count += 1
    return up_count / max(total, 1)


def _safe_get_loc(idx: pd.Index, key: str) -> int:
    """安全获取 iloc 位置，处理重复索引返回 slice 的情况。"""
    loc = idx.get_loc(key)
    if isinstance(loc, slice):
        return loc.start
    if isinstance(loc, (list, np.ndarray)):
        return int(loc[0])
    return int(loc)


def _sym_to_baostock(symbol: str) -> str:
    """将 6 位代码转为 Baostock 格式 (sh/sz.xxxxxx)。"""
    return f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"


def repair_volume_from_baostock(
    cache_dir: str | Path,
    symbols: list[str] | None = None,
    delay: float = BAOSTOCK_DELAY,
    max_stocks: int = 500,
) -> None:
    """用 Baostock 真实 volume 填充缓存中缺失的个股 volume 数据。

    腾讯源不返回 volume，Baostock 作为补全数据源。
    每次请求间隔 delay 秒以防限频。
    """
    import time as _time
    import baostock as _bs

    cache_dir = Path(cache_dir)
    if symbols is None:
        files = sorted(cache_dir.glob("stock_*_daily.parquet"))
        symbols = [f.stem.replace("stock_", "").replace("_daily", "") for f in files]
    symbols = symbols[:max_stocks]

    lg = _bs.login()
    if lg.error_code != "0":
        logger.error(f"Baostock 登录失败: {lg.error_msg}")
        return
    logger.info(f"Baostock 登录成功，修复 {len(symbols)} 只股票 volume...")

    ok = 0
    fail = 0
    for i, sym in enumerate(symbols):
        fpath = cache_dir / f"stock_{sym}_daily.parquet"
        if not fpath.exists():
            fail += 1
            continue

        # 读已有缓存
        df = pd.read_parquet(fpath)
        if df["volume"].notna().sum() > 0:
            ok += 1  # volume 已有数据，跳过
            continue

        # 从 Baostock 获取
        bs_code = _sym_to_baostock(sym)
        start_d = str(df.index[0])[:10]
        end_d = str(df.index[-1])[:10]
        rs = _bs.query_history_k_data_plus(
            bs_code,
            "date,volume",
            start_date=start_d,
            end_date=end_d,
            frequency="d",
            adjustflag="3",
        )
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            fail += 1
            continue

        vol_df = pd.DataFrame(rows, columns=rs.fields)
        vol_df["date"] = pd.to_datetime(vol_df["date"])
        vol_df = vol_df.set_index("date")
        vol_df["volume"] = pd.to_numeric(vol_df["volume"], errors="coerce")

        # 对齐索引填入
        common = df.index.intersection(vol_df.index)
        df.loc[common, "volume"] = vol_df.loc[common, "volume"].astype(
            df["volume"].dtype
        )
        df.to_parquet(fpath)
        ok += 1

        if (i + 1) % 50 == 0:
            logger.info(f"  volume 修复进度: {i+1}/{len(symbols)} (成功 {ok}, 失败 {fail})")

        if delay > 0:
            _time.sleep(delay)

    _bs.logout()
    logger.info(f"volume 修复完成: {ok} 成功, {fail} 失败")

    # 最终验证
    n_still_nan = 0
    for sym in symbols[:100]:
        fpath = cache_dir / f"stock_{sym}_daily.parquet"
        if fpath.exists():
            df = pd.read_parquet(fpath)
            n_still_nan += df["volume"].isna().sum()
    if n_still_nan > 0:
        failed_syms = [s for s in symbols[:100]
                       if pd.read_parquet(cache_dir / f"stock_{s}_daily.parquet")["volume"].isna().sum() > 0]
        logger.warning(f"前100只中仍有 {len(failed_syms)} 只 volume 缺失: {failed_syms[:5]}")
    else:
        logger.info("前100只 volume 已全部修复")


@dataclass
class TradeRecord:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    net_return: float
    holding_days: int
    exit_reason: str = ""
    pattern: str = ""
    sector: str = ""


@dataclass
class L4FractalResult:
    trades: list[TradeRecord] = field(default_factory=list)
    nav: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    n_trades: int = 0
    avg_holding_days: float = 0.0
    avg_pool_size: float = 0.0
    yearly: dict[int, dict] = field(default_factory=dict)
    weekly_l4_pool: dict[str, list[str]] = field(default_factory=dict)
    weekly_sectors: dict[str, list[str]] = field(default_factory=dict)


# ═══════════════════════════════════════════
# 申万行业指数数据获取
# ═══════════════════════════════════════════

def _calc_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """计算 ATR (Average True Range) 百分比。"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    return atr


def fetch_sw_sector_indices(
    cache_dir: str | Path,
    sector_names: list[str] | None = None,
    start_date: str = "20190101",
    end_date: str = "20260508",
) -> dict[str, pd.DataFrame]:
    """行业指数历史 K 线 — 仅从本地 parquet 缓存加载，禁止在线取数。

    返回 {industry_name: OHLCV}。
    缓存文件由 scripts/prefetch_sw_data.py 预先准备好。
    """
    sw_cache = Path(cache_dir) / "sw_index_daily.parquet"
    if not sw_cache.exists():
        logger.error(f"SW指数缓存不存在: {sw_cache}，请先运行 prefetch_sw_data 预取数据")
        return {}

    combined = pd.read_parquet(sw_cache)
    result: dict[str, pd.DataFrame] = {}
    if "sector_name" in combined.columns:
        group_col = "sector_name"
    elif "name" in combined.columns:
        group_col = "name"
    else:
        logger.error("无法识别的SW指数缓存格式")
        return {}

    for n in combined[group_col].unique():
        sub = combined[combined[group_col] == n]
        result[n] = sub.drop(columns=[group_col])
        if not isinstance(result[n].index, pd.DatetimeIndex):
            result[n].index = pd.to_datetime(result[n].index)

    logger.info(f"行业指数从缓存加载: {len(result)} 个行业")
    if sector_names:
        matched = [n for n in sector_names if n in result]
        logger.info(f"  匹配请求: {len(matched)}/{len(sector_names)} 个行业")
        result = {k: v for k, v in result.items() if k in sector_names}
    return result


def _synthesize_concept_prices(
    board_name: str,
    cons_df: pd.DataFrame,
    daily_cache: dict[str, pd.DataFrame],
    min_obs: int = 3,
) -> pd.DataFrame | None:
    """对无申万指数匹配的板块，用成分股 OHLCV 等权平均合成虚拟板块指数。

    返回: DataFrame [date, open, high, low, close, volume]（日期升序）
    """
    frames: list[pd.DataFrame] = []
    for code in cons_df["代码"].astype(str).unique():
        df = daily_cache.get(code)
        if df is None or df.empty:
            continue
        df = df.rename(columns=str.lower).copy()
        required = {"date", "open", "high", "low", "close", "volume"}
        if not required.issubset(set(df.columns)):
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        df["code"] = code
        frames.append(df)

    if not frames:
        logger.info(f"[{board_name}] 无成分股数据，无法合成")
        return None

    all_df = pd.concat(frames, ignore_index=True)

    synth = (
        all_df.groupby("date")[["open", "high", "low", "close", "volume"]]
        .agg(lambda x: x.mean(skipna=True) if x.count() >= min_obs else pd.NA)
        .dropna(how="all")
        .sort_index()
        .reset_index()
    )

    # OHLC 一致性兜底：确保 high >= max(...），low <= min(...）
    synth["high"] = synth[["open", "high", "low", "close"]].max(axis=1)
    synth["low"] = synth[["open", "high", "low", "close"]].min(axis=1)

    return synth



def precompute_sector_momentum(
    sector_names: list[str],
    sector_prices: dict[str, pd.DataFrame],
    biz_days: pd.DatetimeIndex,
    lookback_weeks: int = 8,
) -> dict[str, pd.Series]:
    """预计算板块横截面动量排名 (0-100)。

    在每个周五，计算所有板块过去 lookback_weeks 周的收益率，
    然后进行横截面排名，归一化到 0-100。

    返回: {sector_name: pd.Series(momentum_score, index=fridays)}
    """
    fridays = pd.DatetimeIndex([d for d in biz_days if d.weekday() == 4])

    # 先计算每个板块的周收益率
    weekly_rets: dict[str, pd.Series] = {}
    for name in sector_names:
        df = sector_prices.get(name)
        if df is None or df.empty:
            continue
        weekly = df["close"].resample("W-FRI", closed="right", label="right").last().dropna()
        if len(weekly) < lookback_weeks + 1:
            continue
        ret = weekly.pct_change(lookback_weeks)
        weekly_rets[name] = ret

    # 每个周五做横截面排名
    result: dict[str, pd.Series] = {}
    for name in sector_names:
        if name not in weekly_rets:
            result[name] = pd.Series(50.0, index=fridays)
            continue
        ret = weekly_rets[name]
        mom = pd.Series(50.0, index=fridays)
        for friday in fridays:
            if friday not in ret.index:
                continue
            # 收集所有板块在该周五的收益率
            vals = []
            for n in sector_names:
                if n in weekly_rets and friday in weekly_rets[n].index:
                    vals.append(weekly_rets[n].loc[friday])
            if not vals:
                continue
            this_val = ret.loc[friday]
            if pd.isna(this_val):
                continue
            rank = sum(1 for v in vals if v < this_val) / len(vals)
            mom.loc[friday] = rank * 100
        result[name] = mom
    return result


def precompute_sector_breadth(
    sector_stock_map: dict[str, list[str]],
    store,
    biz_days: pd.DatetimeIndex,
) -> dict[str, pd.Series]:
    """预计算每个板块的广度得分。

    广度 = (涨幅>0个股比例 × 量比>1个股比例) × 100
    衡量板块内是否普涨且放量。

    返回: {sector_name: pd.Series(breadth_score, index=fridays)}
    """
    fridays = pd.DatetimeIndex([d for d in biz_days if d.weekday() == 4])
    result: dict[str, pd.Series] = {}

    for sector_name, stocks in tqdm(sector_stock_map.items(), desc="预计算板块广度"):
        if not stocks:
            continue
        weekly_scores: list[float] = []
        for friday in fridays:
            start = friday - pd.Timedelta(days=6)
            up_count = 0
            vol_up_count = 0
            valid = 0
            for sym in stocks:
                df = store.get_daily(sym)
                if df is None:
                    continue
                # 取周五前后数据（df.loc 自动处理标签范围）
                week_data = df.loc[start:friday]
                if week_data.empty:
                    continue
                # 涨幅判断：最后一天 vs 第一天
                first_close = float(week_data.iloc[0]["close"])
                last_close = float(week_data.iloc[-1]["close"])
                if first_close > 0:
                    valid += 1
                    if last_close > first_close:
                        up_count += 1
                # 量比判断：本周均量 vs 前20日均量
                if len(week_data) >= 2 and "volume" in week_data.columns:
                    avg_vol = week_data["volume"].mean()
                    first_week_idx = _safe_get_loc(df.index, str(week_data.index[0])[:10])
                    if first_week_idx >= 20:
                        vol_ma20 = df["volume"].iloc[first_week_idx-20:first_week_idx].mean()
                    elif first_week_idx > 0:
                        vol_ma20 = df["volume"].iloc[:first_week_idx].mean()
                    else:
                        vol_ma20 = np.nan
                    if not pd.isna(vol_ma20) and avg_vol > vol_ma20:
                        vol_up_count += 1
            if valid >= 3:  # 至少3只有效股票
                breadth = (up_count / valid) * (vol_up_count / max(valid, 1))
                weekly_scores.append(breadth * 100)
            else:
                weekly_scores.append(50.0)
        if weekly_scores:
            result[sector_name] = pd.Series(weekly_scores, index=fridays)
    return result


def rank_sectors(
    store,
    sector_stock_map: dict[str, list[str]],
    as_of: pd.Timestamp,
    top_n: int = 5,
    sector_momentum: dict[str, pd.Series] | None = None,
    sector_breadth_scores: dict[str, pd.Series] | None = None,
) -> list[tuple[str, float]]:
    """L1 板块排名（三维评分系统）。

    动量(40%) + 资金(35%) + 广度(25%)
    资金维度用成交量变异系数代理。

    返回 [(sector_name, score), ...] 按得分降序
    """
    scores: list[tuple[str, float]] = []

    for sector_name in sector_stock_map:
        stocks = sector_stock_map.get(sector_name, [])
        if not stocks:
            continue

        as_of_ts = pd.Timestamp(as_of)

        # ── 动量维度 (35%) ──
        if sector_momentum is not None and sector_name in sector_momentum:
            mom_series = sector_momentum[sector_name]
            recent = mom_series.index[mom_series.index <= as_of_ts]
            mom = float(mom_series.loc[recent[-1]]) if len(recent) > 0 else 50.0
        else:
            mom = 50.0

        # ── 资金维度 (30%)  用成交量变异系数代理 ──
        capital_score = 50.0
        vol_vals = []
        for sym in stocks[:50]:  # 抽样50只控制性能
            df = store.get_daily(sym)
            if df is not None:
                ds = str(as_of)[:10]
                if ds in df.index:
                    v = float(df.loc[ds, "volume"]) if "volume" in df.columns else 0
                    if v > 0:
                        vol_vals.append(v)
        if vol_vals:
            vol_ma = pd.Series(vol_vals).mean()
            vol_cv = pd.Series(vol_vals).std() / max(vol_ma, 1e-8)
            # 低CV=放量一致=资金进场，高CV=分化
            capital_score = max(0, 100 - vol_cv * 50)

        # ── 广度维度 (20%) ──
        if sector_breadth_scores is not None and sector_name in sector_breadth_scores:
            breadth_series = sector_breadth_scores[sector_name]
            recent = breadth_series.index[breadth_series.index <= as_of_ts]
            breadth = float(breadth_series.loc[recent[-1]]) if len(recent) > 0 else 50.0
        else:
            breadth = 50.0

        total = (mom * SECTOR_MOMENTUM_WEIGHT +
                 capital_score * SECTOR_CAPITAL_WEIGHT +
                 breadth * SECTOR_BREADTH_WEIGHT)
        scores.append((sector_name, total))

    if not scores:
        return []

    scores.sort(key=lambda x: -x[1])
    # 前SECTOR_TOP_PERCENTILE比例的板块
    n_select = max(1, int(len(scores) * SECTOR_TOP_PERCENTILE))
    return scores[:n_select]


# ═══════════════════════════════════════════
# L2: 多因子个股评分（替代旧形态/缠论）
# ═══════════════════════════════════════════

def precompute_stock_factors(
    store, symbols: list[str],
) -> dict[str, pd.DataFrame]:
    """预计算8个量价因子，每日截面。

    因子:
      1. 相对强度: 个股4周收益 / 板块4周收益 proxy（用市场平均替代）
      2. 尾盘倾向: (close - low) / (high - low) × 成交额
      3. 量价背离: corr(volume, close, 5日) > 0
      4. 波动率收缩: 20日波动率百分位 < 30%
      5. 换手率异常: volume > MA20 且 < MA20×3
      6. 大单验证: proxy用 volume相对强度
      7. 短期反转: 过去5日涨幅在-3%~3%
      8. 均线多头: MA5>MA10>MA20>MA60

    返回: {symbol: DataFrame(index=date, columns=factor_scores)}
    """
    results: dict[str, pd.DataFrame] = {}

    for sym in tqdm(symbols, desc="预计算8因子"):
        df = store.get_daily(sym)
        if df is None or len(df) < 120:
            continue

        close = df["close"]
        high = df["high"]
        low = df["low"]
        vol = df["volume"].astype(float)

        factors = pd.DataFrame(index=df.index, dtype=float)

        # 1) 动量因子（替代旧RSR）— 20日收益 z-score 化，无未来函数
        ret_4w = close.pct_change(20)
        mom_mean = ret_4w.rolling(252, min_periods=20).mean()
        mom_std = ret_4w.rolling(252, min_periods=20).std().replace(0, float("nan"))
        factors["rsr"] = ((ret_4w - mom_mean) / mom_std.replace(0, float("nan"))).clip(-3, 3)
        factors["rsr"] = (factors["rsr"] + 3) / 6 * 100  # map [-3,3] → [0,100]

        # 2) 尾盘倾向 — 日内位置 (close-low)/(high-low) × 100，无未来函数
        hl_range = (high - low).replace(0, float("nan"))
        factors["tail"] = ((close - low) / hl_range).fillna(0.5) * 100

        # 3) 量价背离 — 5日corr(volume, close)
        vol_ret = vol.pct_change(fill_method=None)
        close_ret = close.pct_change(fill_method=None)
        corr_5 = vol_ret.rolling(5).corr(close_ret)
        factors["vol_price"] = corr_5.fillna(0) * 100

        # 4) 波动率收缩
        vol_20 = close.pct_change(fill_method=None).rolling(20).std()
        vol_rank = vol_20.rolling(60).rank(pct=True)
        factors["volatility"] = ((1 - vol_rank.fillna(0.5)) * 100).clip(0, 100)

        # 5) 换手率异常
        vol_ma20 = vol.rolling(20).mean()
        vol_ratio = vol / vol_ma20.replace(0, float("nan"))
        factors["turnover"] = ((vol_ratio > 1.0) & (vol_ratio < 3.0)).astype(float) * 100

        # 6) 大单验证 proxy — vol相对强度
        vol_rank_20 = vol.rolling(20).rank(pct=True)
        factors["big_order"] = vol_rank_20.fillna(0.5) * 100

        # 7) 短期反转
        ret_5 = close.pct_change(5)
        factors["reversal"] = ((ret_5 > -0.03) & (ret_5 < 0.03)).astype(float) * 100

        # 8) 均线多头
        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        ma_bull = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
        factors["ma_bull"] = ma_bull.astype(float) * 100

        results[sym] = factors.fillna(50.0)

    return results


def screen_stocks_v2(
    candidates: list[str],
    factor_data: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    max_stocks: int = 3,
) -> list[tuple[str, float, str]]:
    """基于8因子评分的个股筛选（V2版）。

    综合分 = Σ(因子分 × 权重) / 总权重
    返回 [(symbol, score, reason), ...] 按评分降序
    """
    as_of_ts = pd.Timestamp(as_of)
    ds = str(as_of_ts)[:10]
    scored: list[tuple[str, float, str]] = []

    weights = {
        "rsr": L3_RSR_WEIGHT,
        "tail": L3_TAIL_WEIGHT,
        "vol_price": L3_VOL_PRICE_WEIGHT,
        "volatility": L3_VOLATILITY_WEIGHT,
        "turnover": L3_TURNOVER_WEIGHT,
        "big_order": L3_BIG_ORDER_WEIGHT,
        "reversal": L3_REVERSAL_WEIGHT,
        "ma_bull": L3_MA_WEIGHT,
    }

    for sym in candidates:
        fd = factor_data.get(sym)
        if fd is None or ds not in fd.index:
            continue

        row = fd.loc[ds]
        total = 0.0
        wsum = 0.0
        active_factors = []
        for name, w in weights.items():
            if name in row.index and not pd.isna(row[name]):
                total += row[name] * w
                wsum += w
                if row[name] > 60:
                    active_factors.append(name)

        if wsum > 0:
            score = total / wsum
            reason = ",".join(active_factors[:3]) if active_factors else "无突出因子"
            scored.append((sym, score, reason))

    scored.sort(key=lambda x: -x[1])
    return scored[:max_stocks]


def precompute_macd_signals(
    store,
    symbols: list[str],
) -> dict[str, dict]:
    """预计算每只股票的标准 MACD 信号（用于 L3 DIF>0 判断）。

    同时预计算 44 日均量（用于 L3 vol≥MA44）和 ATR(14)（诊断用途）。

    返回: {
        sym: {
            "golden_cross": pd.Series(bool),   # 日线MACD金叉
            "death_cross": pd.Series(bool),    # 日线MACD死叉
            "dif": pd.Series(float),           # DIF值（用于L3 DIF>0）
            "dea": pd.Series(float),           # DEA值
            "volume_ma": pd.Series(float),     # 44日均量（用于L3量能确认）
            "atr": pd.Series(float),           # ATR(14) 百分比（诊断）
        }
    }
    """
    results: dict[str, dict] = {}
    for sym in tqdm(symbols, desc="预计算MACD信号"):
        df = store.get_daily(sym)
        if df is None or len(df) < 60:
            continue

        entry: dict = {}
        close = df["close"]

        # 标准 MACD(12,26,9) — 用于 L3 DIF>0 判断
        try:
            dif, dea, _ = calc_macd(close, fast=12, slow=26, signal=9)
            prev_dif = dif.shift(1)
            prev_dea = dea.shift(1)
            entry["golden_cross"] = (prev_dif <= prev_dea) & (dif > dea)
            entry["death_cross"] = (prev_dif >= prev_dea) & (dif < dea)
            entry["dif"] = dif
            entry["dea"] = dea
        except Exception:
            entry["golden_cross"] = pd.Series(False, index=df.index)
            entry["death_cross"] = pd.Series(False, index=df.index)
            entry["dif"] = pd.Series(0.0, index=df.index)
            entry["dea"] = pd.Series(0.0, index=df.index)

        # 44日均量 — 用于 L3 vol ≥ MA(vol, 44) 判断
        try:
            entry["volume_ma"] = df["volume"].rolling(44).mean()
        except Exception:
            entry["volume_ma"] = pd.Series(float("nan"), index=df.index)

        # ATR(14) 百分比 — 诊断用途
        try:
            atr = _calc_atr(df, ATR_PERIOD)
            entry["atr"] = atr / close.where(close > 0, float("nan"))
        except Exception:
            entry["atr"] = pd.Series(float("nan"), index=df.index)

        results[sym] = entry

    return results

# ═══════════════════════════════════════════
# Phase 1: 预计算每周池
# ═══════════════════════════════════════════

def _precompute_weekly_pools(
    store,
    symbols: list[str],
    sector_stock_map: dict[str, list[str]],
    biz_days: pd.DatetimeIndex,
    sector_momentum: dict[str, pd.Series] | None = None,
    sector_breadth: dict[str, pd.Series] | None = None,
    factor_data: dict[str, pd.DataFrame] | None = None,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, set[str]]]:
    """预计算每周的 L4 池和强势板块。

    使用8因子评分 + 板块动量/广度评分。
    factor_data 必须提供（8因子评分已替代旧形态/缠论）。

    返回: (weekly_l4_pool, weekly_sectors, weekly_candidates)
    """
    fridays = sorted(set(
        d for d in biz_days if d.weekday() == 4
    ), key=lambda x: x)

    # Sector ranking for each unique Friday
    weekly_sectors: dict[str, list[str]] = {}
    weekly_candidates: dict[str, set[str]] = {}

    for d in tqdm(fridays, desc="预计算板块排名"):
        wk = d.strftime("%Y-%m-%d")
        top_sectors = rank_sectors(
            store, sector_stock_map, d,
            sector_momentum=sector_momentum,
            sector_breadth_scores=sector_breadth,
        )
        strong = [s for s, _ in top_sectors]
        weekly_sectors[wk] = strong
        candidates: set[str] = set()
        for sec_name in strong:
            for sym in sector_stock_map.get(sec_name, []):
                candidates.add(sym)
        weekly_candidates[wk] = candidates

    # L4 pool screening for each unique Friday — 使用8因子评分
    weekly_l4_pool: dict[str, list[str]] = {}
    for wk_str, candidates in tqdm(weekly_candidates.items(), desc="预计算L4池"):
        if not candidates:
            weekly_l4_pool[wk_str] = []
            continue
        wk_date = pd.Timestamp(wk_str)

        if factor_data is not None:
            scored = screen_stocks_v2(
                list(candidates), factor_data, wk_date,
                max_stocks=MAX_HOLDINGS * 5,
            )
            weekly_l4_pool[wk_str] = [s for s, _, _ in scored]
        else:
            weekly_l4_pool[wk_str] = []

    return weekly_l4_pool, weekly_sectors, weekly_candidates


# ═══════════════════════════════════════════
# Phase 2: 回测模拟
# ═══════════════════════════════════════════

def run_l4_backtest(
    store,
    symbols: list[str],
    sector_stock_map: dict[str, list[str]],
    start: str = "2019-01-01",
    end: str = "2026-05-08",
    max_stocks: int = MAX_HOLDINGS,
    cache_dir: str | Path | None = None,
    diagnose_dir: str | None = None,
) -> L4FractalResult:
    """L4 完整回测（两阶段: 先预计算, 再日频模拟）。"""
    biz_days = pd.bdate_range(start, end)
    if len(biz_days) < 20:
        return L4FractalResult()

    # ── 限制到测试股票池 ──
    test_symbols = set(symbols)
    filtered_sector_map: dict[str, list[str]] = {}
    for sec, ss in sector_stock_map.items():
        filtered = [s for s in ss if s in test_symbols]
        if filtered:
            filtered_sector_map[sec] = filtered
    all_candidate_symbols = set(test_symbols)
    for ss in filtered_sector_map.values():
        all_candidate_symbols.update(ss)
    candidate_list = list(all_candidate_symbols)

    # ═══════════════════════════════════════
    # Phase 1: 预计算
    # ═══════════════════════════════════════

    # L0: 市场状态择时（先于一切）
    logger.info("预计算市场状态...")
    market_state = precompute_market_state(store, biz_days)

    # 1a) 获取申万行业指数（用于板块趋势得分 + 动量计算）
    sector_names = list(filtered_sector_map.keys())
    logger.info(f"获取申万行业指数 ({len(sector_names)} 个板块)...")
    sw_prices = fetch_sw_sector_indices(
        cache_dir or Path.cwd() / "data/raw/_cache/backtest",
        sector_names=sector_names,
        start_date=start.replace("-", ""),
        end_date=end.replace("-", ""),
    )

    # 构建 sector_prices：申万指数优先，无匹配的用合成价格
    sector_prices: dict[str, pd.DataFrame] = {}
    matched_names = []
    unmatched_names = []
    for name in sector_names:
        if name in sw_prices:
            sector_prices[name] = sw_prices[name]
            matched_names.append(name)
        else:
            unmatched_names.append(name)
    if unmatched_names:
        logger.info(f"申万指数匹配: {len(matched_names)}/{len(sector_names)}，剩余 {len(unmatched_names)} 个用合成价格")
        # 预构建个股日线缓存（date 为列）
        _daily_cache: dict[str, pd.DataFrame] = {}
        for sec, stocks in filtered_sector_map.items():
            for sym in stocks:
                if sym not in _daily_cache:
                    d = store.get_daily(sym)
                    if d is not None and not d.empty and "close" in d.columns:
                        _daily_cache[sym] = d.reset_index()  # date index → 列
        synthetic: dict[str, pd.DataFrame] = {}
        for n in unmatched_names:
            cons = pd.DataFrame({"代码": filtered_sector_map[n]})
            df = _synthesize_concept_prices(n, cons, _daily_cache)
            if df is not None:
                df = df.set_index("date")
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
                synthetic[n] = df
        sector_prices.update(synthetic)
    else:
        logger.info(f"申万指数全部匹配: {len(matched_names)}/{len(sector_names)}")

    # 1b) 板块横截面动量（向量化全范围计算）
    logger.info("预计算板块动量...")
    sector_momentum = precompute_sector_momentum(sector_names, sector_prices, biz_days)

    # 1b3) 板块广度评分
    logger.info("预计算板块广度...")
    sector_breadth = precompute_sector_breadth(filtered_sector_map, store, biz_days)

    # 1c) 个股8因子评分（替代旧形态/缠论）
    logger.info("预计算个股8因子...")
    factor_data = precompute_stock_factors(store, candidate_list)

    # 1d) 每周 L4 池 + 强势板块（提前计算所有周五）
    logger.info("预计算每周L4池...")
    weekly_l4_pool, weekly_sectors, weekly_candidates = _precompute_weekly_pools(
        store, candidate_list, filtered_sector_map, biz_days,
        sector_momentum=sector_momentum,
        sector_breadth=sector_breadth,
        factor_data=factor_data,
    )

    # 1e) MACD 信号预计算（用于 Phase 2 买卖信号）
    logger.info("预计算MACD信号...")
    macd_signals = precompute_macd_signals(store, candidate_list)

    # 快速查找
    weekly_pool_sets: dict[str, set[str]] = {
        wk: set(syms) for wk, syms in weekly_l4_pool.items()
    }
    stock_to_sector: dict[str, str] = {}
    for sec, ss in filtered_sector_map.items():
        for s in ss:
            if s not in stock_to_sector:
                stock_to_sector[s] = sec

    # 基准
    bm = store.get_daily("csi300")

    # ── 诊断初始化 ──
    entry_funnel: list = []
    _run_id: str | None = None
    if diagnose_dir:
        from backtest.diagnostic_engine import make_run_id
        _run_id = make_run_id({"n_stocks": len(symbols), "start": start, "end": end, "max_stocks": max_stocks})

    # ═══════════════════════════════════════
    # Phase 2: 日频模拟
    # ═══════════════════════════════════════

    holdings: dict[str, float] = {}        # entry_price
    holdings_date: dict[str, pd.Timestamp] = {}
    holdings_high: dict[str, float] = {}    # 持仓期间最高价（用于移动止损）
    holdings_pool_week: dict[str, str] = {}  # 入池周，防止同周重复买入

    all_trades: list[TradeRecord] = []
    nav = pd.Series(1.0, index=biz_days)
    prev_nav_close: dict[str, float] = {}   # 前一交易日各持仓收盘价（用于链式NAV）
    peak_nav = 1.0
    cooldown_remaining = 0                  # 组合熔断剩余冷却天数

    current_pool: set[str] = set()
    current_strong_sectors: list[str] = []

    # 均线预计算（用于趋势过滤）
    ma_cache: dict[str, pd.DataFrame] = {}

    for i, d in enumerate(tqdm(biz_days, desc="回测模拟")):
        ds = str(d)[:10]
        wk_str = d.strftime("%Y-%m-%d")

        # ── 每日损益跟踪（必须在熔断块之前初始化）──
        daily_realized_pnl = 0.0

        # ── 组合熔断检查 ──
        if cooldown_remaining > 0:
            cooldown_remaining -= 1
            # 冷却期内强制空仓
            for sym in list(holdings.keys()):
                df = store.get_daily(sym)
                if df is not None and ds in df.index:
                    exit_price = float(safe_scalar(df.loc[ds, "close"]))
                    if exit_price > 0:
                        sell_net = exit_price * (1 - SELL_COST)
                        net_ret = sell_net / holdings[sym] - 1
                        daily_realized_pnl += net_ret
                        all_trades.append(TradeRecord(
                            symbol=sym, entry_date=holdings_date[sym], exit_date=d,
                            entry_price=holdings[sym], exit_price=exit_price,
                            net_return=net_ret, holding_days=max(1, (d - holdings_date[sym]).days),
                            exit_reason="组合熔断", sector=stock_to_sector.get(sym, ""),
                        ))
                holdings.pop(sym, None)
                holdings_date.pop(sym, None)
                holdings_high.pop(sym, None)
                holdings_pool_week.pop(sym, None)
            # 冷却期跳过买卖逻辑（NAV 保持前值，循环底部统一的净值段被 continue 跳过）
            nav.iloc[i] = nav.iloc[i - 1] if i > 0 else 1.0
            continue

        # ── L0: 当日市场状态 ──
        day_state = market_state.get(d, "volatile")

        # ── 周五更新池（累加式：保留旧池 + 加入新候选）──
        if d.weekday() == 4:
            new_pool = weekly_pool_sets.get(wk_str, set())
            # 池超限时全量刷新，否则累加
            if len(current_pool) > MAX_HOLDINGS * 5:
                current_pool = new_pool
            else:
                current_pool = current_pool.union(new_pool)
            current_strong_sectors = weekly_sectors.get(wk_str, [])

        # ── 卖出 ──
        for sym in list(holdings.keys()):
            sell = False
            reason = ""
            df = store.get_daily(sym)
            if df is None or ds not in df.index:
                continue

            close = float(safe_scalar(df.loc[ds, "close"]))
            ep = holdings[sym]
            # 更新持仓期间最高收盘价
            if close > holdings_high.get(sym, 0):
                holdings_high[sym] = close
            ed = holdings_date[sym]
            hold_days = max(1, (d - ed).days)
            ms = macd_signals.get(sym)

            # ── 退出条件0: L0市场状态转弱（最高优先级）
            if day_state == "bear":
                sell = True
                reason = "市场转弱清仓"

            # 退出条件1: 硬止损 — 收盘价跌破买入价 5%
            if not sell:
                if close < ep * (1 - MAX_LOSS_PER_TRADE):
                    sell = True
                    reason = "硬止损-5%"

            # 退出条件2: 移动止损 — 收盘价跌破前一日最低价
            if not sell and len(df) > 1:
                prev_idx = _safe_get_loc(df.index, ds) - 1
                if prev_idx >= 0:
                    prev_low = float(safe_scalar(df["low"].iloc[prev_idx]))
                    if not pd.isna(prev_low) and close < prev_low:
                        sell = True
                        reason = "移动止损"

            # 退出条件3: 时间止损 — 持仓 > 40 个交易日且亏损
            if not sell and hold_days > 40 and close < ep:
                sell = True
                reason = "时间止损"

            if sell:
                exit_price = close
                if exit_price > 0:
                    sell_net = exit_price * (1 - SELL_COST)
                    net_ret = sell_net / ep - 1
                    daily_realized_pnl += net_ret
                    all_trades.append(TradeRecord(
                        symbol=sym, entry_date=ed, exit_date=d,
                        entry_price=ep, exit_price=exit_price,
                        net_return=net_ret,
                        holding_days=hold_days,
                        exit_reason=reason, sector=stock_to_sector.get(sym, ""),
                    ))
                holdings.pop(sym, None)
                holdings_date.pop(sym, None)
                holdings_high.pop(sym, None)
                holdings_pool_week.pop(sym, None)

        # ── 买入（受L0市场状态 + 每日止损控制）──
        if day_state == "bear":
            pass
        elif daily_realized_pnl <= -MAX_LOSS_PER_DAY:
            pass
        elif current_strong_sectors and len(holdings) < max_stocks:
            effective_max = MAX_HOLDINGS_VOLATILE if day_state == "volatile" else max_stocks
            slots = effective_max - len(holdings)
            if slots > 0:
                # ── 第一遍：收集所有通过L3条件的候选股 ──
                # passing: list of (sym, close, sector, funnel_idx)
                passing: list[tuple[str, float, str, int]] = []
                for sym in current_pool:
                    from backtest.diagnostic_engine import EntryFunnelRecord
                    fr = EntryFunnelRecord(date=ds, symbol=sym, sector=stock_to_sector.get(sym, ""))

                    if sym in holdings:
                        fr.in_holdings = True; fr.fail_reason = "已持仓"
                        if diagnose_dir: entry_funnel.append(fr)
                        continue
                    if holdings_pool_week.get(sym) == wk_str:
                        fr.same_week_lock = True; fr.fail_reason = "同周已买"
                        if diagnose_dir: entry_funnel.append(fr)
                        continue

                    ms = macd_signals.get(sym)
                    if ms is None:
                        fr.has_ms = False; fr.fail_reason = "无MACD信号"
                        if diagnose_dir: entry_funnel.append(fr)
                        continue

                    df_stock = store.get_daily(sym)
                    if df_stock is None or ds not in df_stock.index:
                        fr.has_data = False; fr.fail_reason = "无行情数据"
                        if diagnose_dir: entry_funnel.append(fr)
                        continue
                    s_close = float(safe_scalar(df_stock.loc[ds, "close"]))
                    s_vol = float(safe_scalar(df_stock.loc[ds, "volume"]))

                    # ── MA数据预计算（MA20/MA60/MA250）──
                    if sym not in ma_cache:
                        if len(df_stock) > 60:
                            c = df_stock["close"]
                            cache = {
                                "ma20": c.rolling(20).mean(),
                                "ma60": c.rolling(60).mean(),
                            }
                            if len(df_stock) > 250:
                                cache["ma250"] = c.rolling(250).mean()
                            ma_cache[sym] = pd.DataFrame(cache)
                        else:
                            fr.ma20_ready = False; fr.fail_reason = "MA数据不足"
                            if diagnose_dir: entry_funnel.append(fr)
                            continue
                    if sym not in ma_cache or ds not in ma_cache[sym].index:
                        continue
                    ma_idx = _safe_get_loc(ma_cache[sym].index, ds)
                    row = ma_cache[sym].iloc[ma_idx]
                    if pd.isna(row.get("ma20", float("nan"))):
                        continue
                    fr.ma20_ready = True
                    if not (s_close > row["ma20"]):
                        fr.fail_reason = "未站上MA20"
                        if diagnose_dir: entry_funnel.append(fr)
                        continue
                    fr.ma20_pass = True
                    if "ma250" in ma_cache[sym].columns:
                        fr.ma250_ready = True
                        try:
                            if ma_idx >= 20:
                                ma250_now = float(row["ma250"])
                                ma250_prev = float(ma_cache[sym].iloc[ma_idx - 20]["ma250"])
                                if not pd.isna(ma250_now) and not pd.isna(ma250_prev) and ma250_now < ma250_prev:
                                    fr.fail_reason = "年线下行"
                                    if diagnose_dir: entry_funnel.append(fr)
                                    continue
                                fr.ma250_pass = True
                        except (KeyError, TypeError):
                            pass
                    # MA250数据不足（<250天）视为通过
                    fr.ma250_pass = True

                    # ═══════════════════════════════════════
                    # 第二层：5条件中至少满足 L3_CONFIRM_MIN 个
                    # ═══════════════════════════════════════
                    confirm = 0

                    # ① MA20 > MA60（中期趋势向上）
                    ma60_val = row.get("ma60", float("nan"))
                    if not pd.isna(ma60_val) and not pd.isna(row["ma20"]) and row["ma20"] > ma60_val:
                        confirm += 1
                        fr.ma_momentum = True

                    # ② close > MA20（站稳短期均线）
                    if s_close > row["ma20"]:
                        confirm += 1
                        fr.ma20_pass = True

                    # ③ 不追高（价格离MA20不超过5%）
                    ma20_dev = abs(s_close / row["ma20"] - 1)
                    fr.near_ma20 = ma20_dev
                    if ma20_dev < MA20_DEVIATION_LIMIT:
                        confirm += 1

                    # ④ DIF > 0（动量为正）
                    if ds in ms["dif"].index:
                        dif_val = float(safe_scalar(ms["dif"].loc[ds]))
                        if not pd.isna(dif_val) and dif_val > 0:
                            confirm += 1
                            fr.dif_gt_0 = True

                    # ⑤ 量能确认 vol ≥ MA(vol, 44)
                    vol_ma = ms.get("volume_ma")
                    if vol_ma is not None and ds in vol_ma.index:
                        vm = float(vol_ma.iloc[_safe_get_loc(vol_ma.index, ds)])
                        if not pd.isna(vm) and s_vol >= vm:
                            confirm += 1
                            fr.vol_ma44_confirm = True

                    fr.confirm_count = confirm
                    if confirm < L3_CONFIRM_MIN:
                        fr.fail_reason = f"条件不足({confirm}/{L3_CONFIRM_MIN})"
                        if diagnose_dir: entry_funnel.append(fr)
                        continue

                    sector = stock_to_sector.get(sym, "")
                    fr.sector_match = sector in current_strong_sectors
                    funnel_idx = len(entry_funnel) if diagnose_dir else -1
                    passing.append((sym, s_close, sector, funnel_idx))
                    if diagnose_dir:
                        entry_funnel.append(fr)

                # ── 第二遍：按因子评分排名选股 ──
                candidates: list[tuple[str, float]] = []
                scored: list[tuple[str, float, float]] = []
                _weights = {
                    "rsr": L3_RSR_WEIGHT, "tail": L3_TAIL_WEIGHT,
                    "vol_price": L3_VOL_PRICE_WEIGHT, "volatility": L3_VOLATILITY_WEIGHT,
                    "turnover": L3_TURNOVER_WEIGHT, "big_order": L3_BIG_ORDER_WEIGHT,
                    "reversal": L3_REVERSAL_WEIGHT, "ma_bull": L3_MA_WEIGHT,
                }
                for sym, sc, sec, _ in passing:
                    fd = factor_data.get(sym)
                    if fd is not None:
                        fds = str(d)[:10]
                        if fds in fd.index:
                            row = fd.loc[fds]
                            total_val = 0.0
                            wsum = 0.0
                            for cname, w in _weights.items():
                                if cname in fd.columns:
                                    v = row[cname]
                                    if not pd.isna(v):
                                        total_val += float(v) * w
                                        wsum += w
                            total_score = total_val / wsum if wsum > 0 else 0.0
                            scored.append((sym, total_score, sc))
                scored.sort(key=lambda x: -x[1])

                for sym, score, sc in scored[:slots]:
                    candidates.append((sym, sc))

                for sym, entry_close in candidates[:slots]:
                    entry_price = entry_close * (1 + BUY_COST)
                    holdings[sym] = entry_price
                    holdings_date[sym] = d
                    holdings_high[sym] = entry_price
                    holdings_pool_week[sym] = wk_str

                # ── 诊断：标记最终买入 ──
                if diagnose_dir:
                    for sym, _, _, idx in passing:
                        if idx >= 0 and idx < len(entry_funnel) and entry_funnel[idx].symbol in holdings:
                            entry_funnel[idx].final_pass = True

        # ── 净值（链式收益率，等权持仓）──
        if i == 0:
            nav.iloc[i] = 1.0
        else:
            daily_rets = []
            for sym, prev_close in prev_nav_close.items():
                df = store.get_daily(sym)
                if df is not None and ds in df.index:
                    curr = float(safe_scalar(df.loc[ds, "close"]))
                    if prev_close > 0 and curr > 0:
                        daily_rets.append(curr / prev_close - 1)
                # 停牌股：prev_nav_close 已保留上一日价格，
                # 但无今日收盘价 → 当日收益为 0（持仓价值不变）
                # 这等价于当日收益 = 0，等权平均中计入一个 0 回报
                else:
                    daily_rets.append(0.0)
            if daily_rets:
                nav.iloc[i] = nav.iloc[i-1] * (1 + np.mean(daily_rets))
            else:
                nav.iloc[i] = nav.iloc[i-1]

            # ── 组合熔断检查（NAV回撤超过阈值）──
            peak_nav = max(peak_nav, nav.iloc[i])
            dd = nav.iloc[i] / peak_nav - 1
            if dd < -PORTFOLIO_DRAWDOWN_LIMIT:
                cooldown_remaining = PORTFOLIO_COOLDOWN_DAYS

        # 保存今日收盘价用于明日NAV计算
        prev_nav_close.clear()
        for sym in holdings:
            df = store.get_daily(sym)
            if df is not None and ds in df.index:
                prev_nav_close[sym] = float(safe_scalar(df.loc[ds, "close"]))
            elif sym in prev_nav_close:
                # 停牌: 保留上一日收盘价，避免权重稀释
                pass  # prev_nav_close 已存在，跳过更新
            else:
                # 首次出现但无数据（不应发生），跳过
                pass

    # ── 统计 ──
    result = _compute_stats(all_trades, nav, bm, weekly_l4_pool)
    result.weekly_sectors = weekly_sectors

    # ── 诊断：保存全链路数据 ──
    if diagnose_dir and _run_id:
        from backtest.diagnostic_engine import save_run
        _bm_ret = float(result.benchmark_return) if hasattr(result, "benchmark_return") and result.benchmark_return else None
        save_run(_run_id, diagnose_dir,
            params={"n_stocks": len(symbols), "start": start, "end": end, "max_stocks": max_stocks},
            market_state=market_state,
            sector_momentum=sector_momentum,
            sector_breadth=sector_breadth,
            weekly_l4_pool=weekly_l4_pool,
            weekly_sectors=weekly_sectors,
            entry_funnel=entry_funnel,
            trades=all_trades,
            nav=nav,
            benchmark_return=_bm_ret,
        )

    return result


# ═══════════════════════════════════════════
# 统计与报告
# ═══════════════════════════════════════════

def _compute_stats(
    trades: list[TradeRecord],
    nav: pd.Series,
    bm: pd.DataFrame | None,
    weekly_l4_pool: dict[str, list[str]],
) -> L4FractalResult:
    result = L4FractalResult()
    # 去重索引（pd.bdate_range 偶尔产生重复日期）
    nav = nav[~nav.index.duplicated(keep="first")]
    daily_ret = nav.pct_change(fill_method=None).dropna()
    if len(daily_ret) < 20:
        return result

    years = len(daily_ret) / 252
    total_ret = float((1 + daily_ret).prod() - 1)
    result.total_return = total_ret
    result.annual_return = float((1 + total_ret) ** (1 / max(years, 0.5)) - 1)
    vol = float(daily_ret.std() * np.sqrt(252))
    result.sharpe_ratio = (result.annual_return - 0.02) / vol if vol > 0 else 0
    result.max_drawdown = float((nav / nav.cummax() - 1).min())
    if bm is not None:
        bc = bm["close"]
        b = bc[~bc.index.duplicated(keep="first")].reindex(nav.index).dropna()
        if len(b) >= 2:
            result.benchmark_return = float(b.iloc[-1] / b.iloc[0] - 1)
            result.excess_return = result.total_return - result.benchmark_return

    if trades:
        rets = np.array([t.net_return for t in trades])
        result.n_trades = len(trades)
        result.win_rate = float((rets > 0).mean())
        gains = rets[rets > 0].sum()
        losses = abs(rets[rets <= 0].sum())
        result.profit_factor = float(gains / losses) if losses > 0 else float("inf")
        result.avg_holding_days = float(np.mean([t.holding_days for t in trades]))

    pool_sizes = [len(v) for v in weekly_l4_pool.values()]
    result.avg_pool_size = float(np.mean(pool_sizes)) if pool_sizes else 0
    result.weekly_l4_pool = weekly_l4_pool

    for t in trades:
        y = t.entry_date.year
        if y not in result.yearly:
            result.yearly[y] = {"trades": 0, "wins": 0, "returns": []}
        result.yearly[y]["trades"] += 1
        if t.net_return > 0:
            result.yearly[y]["wins"] += 1
        result.yearly[y]["returns"].append(t.net_return)

    for y in nav.index.year.unique():
        yn = nav[nav.index.year == y]
        if len(yn) < 20:
            continue
        yd = yn.pct_change(fill_method=None).dropna()
        yret = float(yn.iloc[-1] / yn.iloc[0] - 1)
        yv = float(yd.std() * np.sqrt(252))
        if y not in result.yearly:
            result.yearly[y] = {"trades": 0, "wins": 0, "returns": []}
        result.yearly[y].update({
            "annual_return": yret,
            "max_drawdown": float((yn / yn.cummax() - 1).min()),
            "sharpe": (yret - 0.02) / yv if yv > 0 else 0,
        })

    return result


def print_l4_report(r: L4FractalResult):
    print("=" * 78)
    print("  L4 狙击手 Trade-by-Trade 回测报告 — 5票版（回撤<5%）")
    print("  L0: 市场状态择时(牛≤5只/震荡≤3只/熊0只) | L1: 板块评分(动量40%+资金35%+广度25%)→前20%")
    print("  L2: 8因子个股评分(相对强度+尾盘+量价背离+波动率+换手率+大单+反转+均线多头)→Top5")
    print("  L3: MA250不下行(硬性)+五条件≥4/5(MA20>MA60+close>MA20+不追高5%+DIF>0+vol≥MA44)")
    print("  L4: 三层退出(L0熊市+硬止损-5%+前日最低价移动止损)+组合熔断(回撤3.5%停5天)")
    print("=" * 78)
    print(f"  累计收益:     {r.total_return:>+9.2%}")
    print(f"  年化收益:     {r.annual_return:>+9.2%}")
    print(f"  最大回撤:     {r.max_drawdown:>-9.2%}")
    print(f"  夏普比率:     {r.sharpe_ratio:>9.3f}")
    print(f"  基准收益:     {r.benchmark_return:>+9.2%}")
    print(f"  超额收益:     {r.excess_return:>+9.2%}")
    print(f"  ──────────────────────────────")
    print(f"  总交易次数:   {r.n_trades:>9}")
    print(f"  胜率:         {r.win_rate:>9.1%}")
    print(f"  盈亏比:       {r.profit_factor:>9.2f}")
    print(f"  平均持仓:     {r.avg_holding_days:>9.0f}天")
    print(f"  L4池均规模:   {r.avg_pool_size:>9.1f}")

    if r.yearly:
        print()
        print("  ╔" + "═" * 78 + "╗")
        print("  ║" + "  按年收益".center(68) + "║")
        print("  ╠" + "═" * 78 + "╣")
        print(f"  ║ {'年份':^6} │ {'年收益':>8} │ {'基准':>8} │ {'超额':>8} │ "
              f"{'夏普':>6} │ {'回撤':>7} │ {'交易':>5} │ {'胜率':>6} ║")
        print("  ╟" + "─" * 78 + "╢")
        for wy in sorted(r.yearly.keys()):
            ys = r.yearly[wy]
            yret = ys.get("annual_return", 0)
            ysh = ys.get("sharpe", 0)
            ydd = ys.get("max_drawdown", 0)
            bmr = yret - ys.get("excess_return", 0) if "excess_return" in ys else 0
            nt = ys.get("trades", 0)
            wr = ys.get("wins", 0) / max(nt, 1)
            if "annual_return" in ys:
                print(f"  ║ {wy:^6} │ {yret:>+7.2%} │ {bmr:>+7.2%} │ "
                      f"{yret - bmr:>+7.2%} │ {ysh:>5.2f} │ {ydd:>6.2%} │ "
                      f"{nt:>5} │ {wr:>5.1%} ║")
        print("  ╚" + "═" * 78 + "╝")

    if r.trades:
        sorted_trades = sorted(r.trades, key=lambda t: t.net_return)
        if len(sorted_trades) >= 5:
            print(f"\n  ── Top 5 最佳 ──")
            for t in sorted_trades[-5:][::-1]:
                print(f"  {t.symbol} {str(t.entry_date)[:10]}→{str(t.exit_date)[:10]} "
                      f"{t.holding_days:>3}d {t.net_return:>+7.2%} [{t.exit_reason}]")
            print(f"\n  ── Top 5 最差 ──")
            for t in sorted_trades[:5]:
                print(f"  {t.symbol} {str(t.entry_date)[:10]}→{str(t.exit_date)[:10]} "
                      f"{t.holding_days:>3}d {t.net_return:>+7.2%} [{t.exit_reason}]")
    print()
