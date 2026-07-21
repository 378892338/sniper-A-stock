"""全局参数配置 — 冻结数据类，全参数化"""

from dataclasses import dataclass

# ── 因子驱动 Schema 定义 ──
# 数据格式标准 = 因子需要什么，数据库就存什么。
# 当因子开发者增加新字段时，只改这个列表，
# 其余自动对齐（ALTER TABLE + Normalizer + 质量校验）。
FACTOR_REQUIRED_FIELDS = ["open", "high", "low", "close", "volume", "amount"]
FACTOR_DESIRED_FIELDS = ["turnover"]


@dataclass(frozen=True)
class MarketConfig:
    """L0 市场状态评分参数"""
    trend_window: int = 20
    volume_window: int = 20
    breadth_window: int = 20
    northbound_window: int = 5
    trend_weight: float = 0.40          # 多周期趋势（MA250+MA20/60）
    volume_weight: float = 0.30         # 量能
    breadth_weight: float = 0.20        # 宽度
    northbound_weight: float = 0.10     # 北向（常缺失，降权）
    bullish_threshold: float = 60.0     # 2026-06-30 最终确认: L0≥60开仓，单层SW1最优
    bearish_threshold: float = 30.0     # ≤30 → 熊市


@dataclass(frozen=True)
class SectorConfig:
    """L1 板块评分参数"""
    momentum_window: int = 5
    volume_surge_threshold: float = 1.5
    momentum_weight: float = 0.35
    fund_flow_weight: float = 0.25
    breadth_weight: float = 0.20
    heat_weight: float = 0.20
    top_n: int = 5
    top_n_high: int = 3   # 2026-06-06 Strategy A: 统一取前3（原5）
    top_n_low: int = 3     # 2026-06-06 Strategy A: 统一取前3（不变）


@dataclass(frozen=True)
class StockConfig:
    """L2 个股评分参数

    权重设计:
      - 技术因子（趋势/量能/MACD/RSI/市值/底分型/量价反转/低波动）: 75%
        始终有数据，提供基础区分度
      - 资金因子（资金流/大单/龙虎榜）: 13%
        数据稀疏，有则加分，无则自动跳过（NaN-aware）
      - 基本面因子（EPS/ROE/营收增长）: 12%
        季度更新，有则加分，无则自动跳过（NaN-aware）

    2026-06-29 因子扩展:
      - 新增 bottom_fractal(底分型), volume_reversal(量价反转), low_volatility(低波动率)
      - 删除 turnover_weight（daily_bars 无换手率列，恒 50.0 死因子）
      - 资金面/基本面各压缩 2%/3% 释放权重给新技术因子
    """
    trend_factor_weight: float = 0.25
    volume_factor_weight: float = 0.15
    macd_factor_weight: float = 0.15
    rsi_factor_weight: float = 0.08
    market_cap_weight: float = 0.04
    # 2026-06-29 新增: 形态识别
    bottom_fractal_weight: float = 0.03
    # 2026-06-29 新增: 量价背离反转
    volume_reversal_weight: float = 0.02
    # 2026-06-29 新增: 低波动率 alpha
    low_volatility_weight: float = 0.03
    fund_flow_weight: float = 0.06
    big_order_weight: float = 0.04
    dragon_tiger_weight: float = 0.03
    eps_weight: float = 0.06
    roe_weight: float = 0.03
    revenue_growth_weight: float = 0.03
    momentum_window: int = 10
    rsi_window: int = 14
    top_n: int = 10


@dataclass(frozen=True)
class EntryConfig:
    """L3 入场条件参数"""
    hard_min_price: float = 3.0
    hard_max_price: float = 300.0
    hard_min_volume: float = 1e6
    hard_max_turnover: float = 0.30
    hard_not_limit_up: bool = True
    soft_min_score: float = 61.0       # Optuna 贝叶斯调优 2026-06-11（原 79.0）
    soft_sector_top: int = 3


@dataclass(frozen=True)
class ExitConfig:
    """L4 退出链参数

    两层止损（2026-06-05 起固定规则，不参与打字机归因）:
      - stop_loss: 未盈利阶段 → 日内最低价跌破 -2% 即止损
      - trailing_stop: 脱离成本后 → 从最高点回撤 -3% 动态止盈
    """
    stop_loss: float = -0.02            # Optuna 贝叶斯调优 2026-06-11（原 -0.03）
    trailing_stop: float = -0.03        # Optuna 贝叶斯调优 2026-06-11（原 -0.05）
    max_hold_days: int = 10             # Optuna 贝叶斯调优 2026-06-11（原 43）
    ma_break_below: int = 20


@dataclass(frozen=True)
class RiskConfig:
    """风控参数"""
    max_positions: int = 5
    position_size: float = 0.07          # 打靶优化 2026-06-04（原 0.12）
    target_exposure_ratio: float = 0.50  # 2026-06-05 首次开仓目标（总资金50%）
    max_sector_exposure: float = 0.40
    max_daily_loss: float = -0.03
    max_total_loss: float = -0.20       # 提前风控
    portfolio_drawdown_limit: float = -0.05  # 2026-06-06 组合回撤5%强制减仓
    min_hold_days: int = 1
    active_reduction_l0: float = 60.0             # 与 bullish_threshold 同步
    active_reduction_exposure: float = 0.30     # 主动减仓目标总暴露


@dataclass(frozen=True)
class BacktestConfig:
    """回测参数"""
    start_date: str = "2019-01-01"
    end_date: str = "2026-05-13"
    initial_capital: float = 1_000_000
    commission_buy: float = 0.00025    # 交易佣金 0.025%（普遍可谈的费率）
    commission_sell: float = 0.00025   # 交易佣金 0.025%
    stamp_duty: float = 0.0005         # 印花税 0.05%（仅卖出）
    slippage: float = 0.001            # 滑点 0.1%（双边）
    min_win_rate: float = 0.50
    annual_target: float = 0.30


MARKET = MarketConfig()
SECTOR = SectorConfig()
STOCK = StockConfig()
ENTRY = EntryConfig()
EXIT = ExitConfig()
RISK = RiskConfig()
BACKTEST = BacktestConfig()

# ── ETF 动量评分参数 ──

@dataclass(frozen=True)
class EtfMomentumConfig:
    """ETF动量评分权重 — 全部有语义锚点"""
    # 4维评分权重
    w_high_proximity: float = 0.40     # 60日新高接近度
    w_ma60_deviation: float = 0.30     # MA60偏离度
    w_fund_validation: float = 0.20    # 资金验证
    w_continuation: float = 0.10       # 延续确认
    # 窗口参数
    window_high: int = 60
    window_ma: int = 60
    window_fund: int = 20
    window_cont: int = 5
    freshness_hours: int = 24
    # 60日新高衰减(评审WARN项修复: 连续创新高导致信号饱和)
    high_decay_enabled: bool = True
    high_decay_start: int = 3           # 连续3天后开始衰减
    high_decay_min: float = 0.3         # 最低保留30%
    high_decay_rate: float = 0.1        # 每天衰减10%
    # 数值稳定性
    epsilon: float = 1e-8

    def __post_init__(self):
        assert 0 <= self.w_high_proximity <= 1
        assert 0 <= self.high_decay_min <= 1
        assert self.high_decay_start >= 1


@dataclass(frozen=True)
class FusionConfig:
    """融合引擎超参数 — l0_min与MarketConfig锚点同步"""
    # L0-gated 市场状态锚点
    l0_min: float = MARKET.bearish_threshold   # 引用MarketConfig,防脱同步
    l0_max: float = 80.0                        # ETF权重饱和天花板
    # ETF先验权重边界
    w_etf_min: float = 0.10
    w_etf_max: float = 0.70
    # 贝叶斯精度映射
    prior_precision: float = 1.0               # SW1后验权重>=65%, ETF<=35%
    signal_scale: float = 25.0                 # signal_gain最大值0.762
    # 数值保护
    epsilon: float = 1e-8

    # === 门控优化新增(终审APPROVED v1.1) ===
    # 门控模式: "linear"(默认,当前行为)|"humpback"|"humpback_cv"|"full"
    gating_mode: str = "linear"
    # 驼峰参数(μ=66精确锚定全周期L0中位数)
    humpback_mu: float = 66.0
    sigma_left: float = 18.0      # 熊市侧带宽(更宽,保留ETF避险)
    sigma_right: float = 9.0      # 牛市侧带宽(更窄,快速压制饱和)
    w_floor_global: float = 0.10  # 全局硬地板(ETF永不彻底失效)

    # CV截面饱和检测
    cv_enabled: bool = True
    cv_low: float = 0.03    # CV<=此值->信号饱和->g_cv=floor
    cv_high: float = 0.12   # CV>=此值->信号健康->g_cv=1.0
    g_cv_floor: float = 0.20  # CV门控地板(永不完全关停)

    # 波动率自适应(默认关闭,Phase4验证后开启)
    vol_enabled: bool = False
    vol_mid: float = 0.22     # Sigmoid中点(~22%年化波动率)
    vol_steep: float = 0.05   # Sigmoid陡峭度
    vol_min: float = 0.60     # 低波乘数下限
    vol_max: float = 1.10     # 高波乘数上限

    def __post_init__(self):
        assert self.l0_min < self.l0_max, "l0_min must be < l0_max"
        assert 0 <= self.w_etf_min < self.w_etf_max <= 1.0
        assert self.prior_precision > 0
        assert self.gating_mode in ("linear", "humpback", "humpback_cv", "full"), \
            f"gating_mode={self.gating_mode} not in (linear,humpback,humpback_cv,full)"
        assert 0 < self.cv_low < self.cv_high, f"cv_low<cv_high required: {self.cv_low}>={self.cv_high}"
        assert 0 < self.g_cv_floor < 1.0
        if self.gating_mode == "linear":
            assert self.w_floor_global <= self.w_etf_min, \
                f"linear模式: w_floor_global({self.w_floor_global})必须<=w_etf_min({self.w_etf_min})"


@dataclass(frozen=True)
class DdrConfig:
    """DDR分歧诊断参数 — 纯诊断不调权"""
    convergent_threshold: float = 0.5   # |delta_z| < 0.5 -> CONVERGENT
    leading_threshold: float = 1.5      # |delta_z| > 1.5 -> ETF/SW1_LEADING
    coverage_gap_enabled: bool = True   # 监控ETF覆盖偏差

    def __post_init__(self):
        assert 0 < self.convergent_threshold < self.leading_threshold


@dataclass(frozen=True)
class DegradationConfig:
    """降级仲裁与恢复条件参数"""
    # 恢复条件(评审FAIL-15修复: 全量化退出条件)
    yellow_to_green_days: int = 3
    orange_to_yellow_days: int = 2
    red_to_orange_days: int = 1
    orange_to_yellow_w_etf_ratio: float = 0.5
    smooth_transition_days: int = 2
    # 同步屏障(评审FAIL-16修复: ThreadPoolExecutor取代asyncio)
    etf_timeout_seconds: int = 120
    sw1_timeout_seconds: int = 120
    hard_deadline: str = "15:10"
    # 冷启动(评审FAIL-18修复)
    warmup_days: int = 5
    max_line_bytes: int = 4096
    rotate_by_date: bool = True
    # 纸带(评审FAIL-16修复: 含崩溃恢复)
    orphan_draft_hours: int = 48        # 覆盖周五->周一跨周末窗口

    def __post_init__(self):
        assert self.yellow_to_green_days >= 1
        assert self.orange_to_yellow_days >= 1
        assert self.warmup_days >= 3


ETF_MOMENTUM = EtfMomentumConfig()
FUSION = FusionConfig()
DDR = DdrConfig()
DEGRADATION = DegradationConfig()

# ═══════════════════════════════════════════════════════════════
# 打字机归因 — 运行时动态参数切换
# ═══════════════════════════════════════════════════════════════
# 通过 L0 子维度最近邻匹配，在纸带上找最相似的历史交易
# 归因 → 切换参数 → 交易 → 纸带追加

import json as _json
import os as _os
import sqlite3 as _sqlite3
import numpy as _np
import pandas as _pd

from core.logger import get_logger
_logger = get_logger("sniper.config")

# ── 纸带 sqlite 连接（延迟初始化） ──
_TAPE_CONN: _sqlite3.Connection | None = None
_TAPE_FLUSH_COUNT = 0
_TAPE_FLUSH_THRESHOLD = 100

def _get_tape_conn() -> _sqlite3.Connection:
    """获取纸带 sqlite 连接的延迟初始化。"""
    global _TAPE_CONN
    if _TAPE_CONN is None:
        try:
            from config.paths import TAPE_DIR
            db_path = TAPE_DIR / "paper_tape.db"
        except ImportError:
            db_path = "outputs/paper_tape.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _TAPE_CONN = _sqlite3.connect(str(db_path))
        _TAPE_CONN.execute("""
            CREATE TABLE IF NOT EXISTS paper_tape (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pnl_pct REAL,
                entry_date TEXT,
                exit_date TEXT,
                hold_days INTEGER,
                exit_reason TEXT,
                symbol TEXT,
                l0_score REAL,
                l0_trend REAL,
                l0_volume REAL,
                l0_breadth REAL,
                config_snapshot TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        _TAPE_CONN.commit()
        _logger.info(f"纸带 sqlite 已打开: {db_path}")
    return _TAPE_CONN

_TRADE_PAPER: list[dict] | None = None  # 纸带（内存缓存）
_DISTANCE_WEIGHTS = [0.50, 0.20, 0.15, 0.15]  # L0, 趋势, 量能, 宽度

# ── 全参数快照 — 107 个 Config 字段全部进入纸带 ──
_ALL_CONFIG_CLASSES: list[tuple[str, type]] = [
    ("MarketConfig", MarketConfig),
    ("SectorConfig", SectorConfig),
    ("StockConfig", StockConfig),
    ("EntryConfig", EntryConfig),
    ("ExitConfig", ExitConfig),
    ("RiskConfig", RiskConfig),
    ("BacktestConfig", BacktestConfig),
    ("EtfMomentumConfig", EtfMomentumConfig),
    ("FusionConfig", FusionConfig),
    ("DdrConfig", DdrConfig),
    ("DegradationConfig", DegradationConfig),
]


def snapshot_all_params() -> dict[str, float]:
    """快照当前全部 Config 参数的扁平化 dict。

    Returns:
        {"MarketConfig_trend_window": 20, "ExitConfig_stop_loss": -0.02, ...}
        共 107 个键。
    """
    snap: dict[str, float] = {}
    config_map = {
        "MarketConfig": MARKET,
        "ExitConfig": EXIT,
        "RiskConfig": RISK,
        "EntryConfig": ENTRY,
        "BacktestConfig": BACKTEST,
        "SectorConfig": SECTOR,
        "StockConfig": STOCK,
        "EtfMomentumConfig": ETF_MOMENTUM,
        "FusionConfig": FUSION,
        "DdrConfig": DDR,
        "DegradationConfig": DEGRADATION,
    }
    for config_name, config_obj in config_map.items():
        for field, val in config_obj.__dict__.items():
            snap[f"{config_name}_{field}"] = val
    return snap

# ── 打字机归因参数元数据 ──
# 每个参数的定义域：lo/hi（边界）、step（步长）、int（是否整数）
_PARAMS_META = {
    "max_hold_days":    {"lo": 5,     "hi": 60,    "step": 5,    "int": True},
    "position_size":    {"lo": 0.05,  "hi": 0.25,  "step": 0.01, "int": False},
    "soft_min_score":   {"lo": 48,    "hi": 80,    "step": 2,    "int": True},
    # 🔒 bullish_threshold 不参与动态优化（L0≥60 固定开仓线）
}

# 参数名 → Config 类映射（run_live.py 快照等外部调用需要）
_PARAM_TO_CONFIG = {
    "max_hold_days":     "EXIT",
    "position_size":     "RISK",
    "soft_min_score":    "ENTRY",
    # 🔒 bullish_threshold 不参与动态优化
}


def load_paper_tape(path: str = "") -> None:
    """启动时加载纸带（一次调用）。

    默认从 sqlite 读取（paper_tape.db），path_override 时从 parquet 读（测试兼容）。
    将展平数据重建为 params / market_state_vector / all_params 嵌套结构。
    """
    global _TRADE_PAPER

    if path:
        # path_override → 从 parquet 读（测试兼容）
        if not _os.path.exists(path):
            return
        df = _pd.read_parquet(path)
        records = df.to_dict("records")
    else:
        # 默认从 sqlite 读
        conn = _get_tape_conn()
        try:
            df = _pd.read_sql("SELECT * FROM paper_tape ORDER BY row_id", conn)
        except Exception:
            return
        if df.empty:
            return
        records = df.to_dict("records")
        # 合并 config_snapshot JSON 列
        for r in records:
            cs = r.pop("config_snapshot", "{}") or "{}"
            try:
                all_p = _json.loads(cs)
                if all_p:
                    r["all_params"] = all_p
            except Exception:
                pass

    for r in records:
        # 重建 params dict：param_stop_loss → params["stop_loss"]（向后兼容）
        param_keys = [k for k in r if k.startswith("param_")]
        if param_keys:
            r["params"] = {k.replace("param_", ""): r.pop(k) for k in param_keys}

        # 重建 market_state_vector：[msv_l0, msv_trend, msv_volume, msv_breadth]
        if "msv_l0" in r:
            r["market_state_vector"] = [
                r.pop("msv_l0"), r.pop("msv_trend", 50.0),
                r.pop("msv_volume", 50.0), r.pop("msv_breadth", 50.0),
            ]
        # 重建 all_params：从 ConfigName_field 列（parquet 旧格式）
        if "all_params" not in r:
            all_p = {}
            for k in list(r.keys()):
                if k.startswith("MarketConfig_") or k.startswith("ExitConfig_") or \
                   k.startswith("RiskConfig_") or k.startswith("EntryConfig_") or \
                   k.startswith("BacktestConfig_") or k.startswith("SectorConfig_") or \
                   k.startswith("StockConfig_") or k.startswith("EtfMomentumConfig_") or \
                   k.startswith("FusionConfig_") or k.startswith("DdrConfig_") or \
                   k.startswith("DegradationConfig_"):
                    all_p[k] = r.pop(k)
            if all_p:
                r["all_params"] = all_p

    _TRADE_PAPER = records
    # 飞轮闭环：加载持久化参数（恢复上次归因结果）
    load_effective_params()


def append_to_paper_tape(trade: dict, path_override: str = "") -> None:
    """将一笔完成的交易追加到纸带。

    默认走 sqlite INSERT（O(1) IO，单笔 ~0.5ms）。
    path_override 时走 parquet 全量重写（测试兼容）。

    Args:
        trade: 交易记录（必须含 params, market_state_vector, pnl_pct 等）
        path_override: 测试时指定临时 parquet 路径
    """
    global _TRADE_PAPER, _TAPE_FLUSH_COUNT

    # ── 展平 ──
    row: dict = {}
    for k, v in trade.items():
        if k in ("params", "market_state_vector"):
            continue
        row[k] = v
    for k, v in trade.get("params", {}).items():
        row[f"param_{k}"] = v
    vec = trade.get("market_state_vector", [50.0, 50.0, 50.0, 50.0])
    row["msv_l0"] = vec[0]
    row["msv_trend"] = vec[1] if len(vec) > 1 else 50.0
    row["msv_volume"] = vec[2] if len(vec) > 2 else 50.0
    row["msv_breadth"] = vec[3] if len(vec) > 3 else 50.0

    # 全量 Config 参数快照 → JSON
    config_snapshot = _json.dumps(snapshot_all_params(), ensure_ascii=False)

    if path_override:
        # ── path_override → 走 parquet 全量重写（测试兼容） ──
        new_df = _pd.DataFrame([row])
        if _os.path.exists(path_override):
            existing = _pd.read_parquet(path_override)
            for col in existing.columns:
                if col not in new_df.columns:
                    new_df[col] = None
            combined = _pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
        combined.to_parquet(path_override, index=False)
    else:
        # ── 默认走 sqlite INSERT（O(1) IO） ──
        conn = _get_tape_conn()
        conn.execute(
            """INSERT INTO paper_tape
               (pnl_pct, entry_date, exit_date, hold_days, exit_reason, symbol,
                l0_score, l0_trend, l0_volume, l0_breadth, config_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("pnl_pct"),
                row.get("entry_date", ""),
                row.get("exit_date", ""),
                row.get("hold_days", 0),
                row.get("exit_reason", ""),
                row.get("symbol", ""),
                row.get("l0_score", 50.0),
                row.get("l0_trend", 50.0),
                row.get("l0_volume", 50.0),
                row.get("l0_breadth", 50.0),
                config_snapshot,
            ),
        )
        _TAPE_FLUSH_COUNT += 1
        if _TAPE_FLUSH_COUNT >= _TAPE_FLUSH_THRESHOLD:
            conn.commit()
            _TAPE_FLUSH_COUNT = 0

    # ── 更新内存缓存 ──
    if _TRADE_PAPER is not None:
        reconstructed = {
            "pnl_pct": row.get("pnl_pct"),
            "l0_score": row.get("l0_score", 50.0),
            "l0_trend": row.get("l0_trend", 50.0),
            "l0_volume": row.get("l0_volume", 50.0),
            "l0_breadth": row.get("l0_breadth", 50.0),
            "entry_date": row.get("entry_date", ""),
            "exit_date": row.get("exit_date", ""),
            "hold_days": row.get("hold_days", 0),
            "exit_reason": row.get("exit_reason", ""),
            "symbol": row.get("symbol", ""),
            "market_state_vector": [
                row.get("l0_score", 50.0), row.get("l0_trend", 50.0),
                row.get("l0_volume", 50.0), row.get("l0_breadth", 50.0),
            ],
            "all_params": _json.loads(config_snapshot),
        }
        # 从 params 快照重建旧版 params 字段（向后兼容）
        param_keys = [k for k in row if k.startswith("param_")]
        if param_keys:
            reconstructed["params"] = {k.replace("param_", ""): row[k] for k in param_keys}

        _TRADE_PAPER.append(reconstructed)


def flush_paper_tape() -> None:
    """强制 flush 纸带 buffer（程序结束前调用）。"""
    global _TAPE_FLUSH_COUNT
    if _TAPE_CONN and _TAPE_FLUSH_COUNT > 0:
        _TAPE_CONN.commit()
        _TAPE_FLUSH_COUNT = 0
        _logger.info("[飞轮] 纸带 sqlite 已 flush")
                reconstructed.pop("msv_l0"),
                reconstructed.pop("msv_trend", 50.0),
                reconstructed.pop("msv_volume", 50.0),
                reconstructed.pop("msv_breadth", 50.0),
            ]
        _TRADE_PAPER.append(reconstructed)


def _effective_params_path():
    """飞轮参数持久化路径（数据仓 optimize_target）。"""
    try:
        from config.paths import OUTPUT_DIR
        return OUTPUT_DIR / "optimize_target" / ".effective_params.json"
    except ImportError:
        from pathlib import Path as _Path
        return _Path("outputs/optimize_target/.effective_params.json")


def save_effective_params() -> None:
    """持久化当前全局参数到磁盘（飞轮闭环：归因结果不因进程退出丢失）。"""
    p = _effective_params_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    import datetime as _dt_mod
    snapshot = {
        "EXIT.stop_loss": EXIT.stop_loss,
        "EXIT.trailing_stop": EXIT.trailing_stop,
        "EXIT.max_hold_days": EXIT.max_hold_days,
        "RISK.position_size": RISK.position_size,
        "RISK.max_positions": RISK.max_positions,
        "ENTRY.soft_min_score": ENTRY.soft_min_score,
        "MARKET.bullish_threshold": MARKET.bullish_threshold,
        "MARKET.bearish_threshold": MARKET.bearish_threshold,
        "last_updated": _dt_mod.datetime.now().isoformat(),
    }
    p.write_text(
        _json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _logger.info(f"飞轮参数已持久化: {p}")


def load_effective_params(path: str = "") -> None:
    """从磁盘恢复持久化参数到全局配置（飞轮闭环：进程重启后继承上次归因结果）。"""
    global EXIT, RISK, ENTRY, MARKET
    p = _Path(path) if path else _effective_params_path()
    if not p.exists():
        return
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
        params: dict[str, dict] = {"EXIT": {}, "RISK": {}, "ENTRY": {}, "MARKET": {}}
        for key, val in data.items():
            if key == "last_updated":
                continue
            section, param = key.split(".", 1)
            if section in params:
                params[section][param] = val
        cls_map = {
            "EXIT": ExitConfig, "RISK": RiskConfig,
            "ENTRY": EntryConfig, "MARKET": MarketConfig,
        }
        refs = {"EXIT": EXIT, "RISK": RISK, "ENTRY": ENTRY, "MARKET": MARKET}
        for name, cls in cls_map.items():
            pv = params.get(name, {})
            if not pv:
                continue
            merged = {**refs[name].__dict__, **pv}
            globals()[name] = cls(**merged)
        _logger.info(
            f"飞轮参数已从 {p} 恢复（{data.get('last_updated', '?')}）"
        )
    except Exception as e:
        _logger.warning(f"飞轮参数加载失败（使用默认值）: {e}")


def _market_distance(fp1: list[float], fp2: list[float]) -> float:
    """加权欧氏距离。"""
    diff = _np.array(fp1) - _np.array(fp2)
    return float(_np.sqrt(_np.sum(_np.array(_DISTANCE_WEIGHTS) * diff ** 2)))


def _find_neighbors(today_fp: list[float]) -> list[dict]:
    """在纸带上找距离最近的 N 笔交易。"""
    if _TRADE_PAPER is None or len(_TRADE_PAPER) < 10:
        return []
    # 动态 N：纸带越多，取越稳定
    total = len(_TRADE_PAPER)
    if total < 50:
        n = total
    elif total < 200:
        n = min(40, total)
    elif total < 500:
        n = min(35, total)
    else:
        n = 30

    distances = [
        (_market_distance(today_fp,
                          t.get("market_state_vector") or [float("inf")] * 4), t)
        for t in _TRADE_PAPER
    ]
    distances.sort(key=lambda x: x[0])
    neighbors = [t for _, t in distances[:n]]
    avg_dist = float(_np.mean([d for d, _ in distances[:n]]))
    _logger.info(f"纸带近邻: 纸带={total}笔, 取n={n}, 平均距离={avg_dist:.4f}")
    return neighbors


def profit_impact(trades: list[dict], param_k: str) -> dict:
    """P&L 自然分组归因公式。

    以 PnL=0 为天然分界线：
      - 赚钱组 (PnL > 0)：参数值的中位数 = win_median
      - 亏钱组 (PnL ≤ 0)：参数值的中位数 = lose_median

    impact = win_median - lose_median
      > 0：参数偏大有利，< 0：参数偏小有利，≈ 0：无区分度

    返回:
      impact: 正值 → 赚钱组参数值更大（偏大有利）
      win_median: 赚钱组参数值中位数（靶心参考方向）
      lose_median: 亏钱组参数值中位数
    """
    vals = _np.array([t["params"].get(param_k, 0) for t in trades], dtype=float)
    pnls = _np.array([t["pnl_pct"] for t in trades], dtype=float)

    # 过滤 NaN PnL（数据异常、字段缺失、手动构造交易）
    valid = ~_np.isnan(pnls)
    vals = vals[valid]
    pnls = pnls[valid]

    if len(vals) < 10:
        return {"impact": 0.0, "win_median": 0.0, "lose_median": 0.0}

    # PnL 自然分组：PnL>0 赚钱组，PnL≤0 亏钱组
    win_mask = pnls > 0
    lose_mask = ~win_mask

    n_win = int(win_mask.sum())
    n_lose = int(lose_mask.sum())

    # 任一组 < 3 笔 → 无区分度
    if n_win < 3 or n_lose < 3:
        return {"impact": 0.0, "win_median": 0.0, "lose_median": 0.0}

    win_vals = vals[win_mask]
    lose_vals = vals[lose_mask]

    win_median = float(_np.median(win_vals))
    lose_median = float(_np.median(lose_vals))
    impact = round(win_median - lose_median, 6)

    return {
        "impact": impact,
        "win_median": round(win_median, 4),
        "lose_median": round(lose_median, 4),
    }


def _snap_to_grid(val: float, meta: dict) -> float:
    """将值约束到合法步长网格上。

    先 snap 后 clamp：先找到最近的网格点，再约束到 [lo, hi] 范围内。
    （若先 clamp 后 snap，hi 不能被 step 整除时会越界。）
    """
    lo, hi, step = meta["lo"], meta["hi"], meta["step"]
    snapped = round(val / step) * step
    if meta["int"]:
        snapped = int(snapped)
    return max(lo, min(hi, snapped))


def _attribution(trades: list[dict],
                 params_meta: dict | None = None) -> dict:
    """对交易集做并行归因，返回有信号的参数（B方案）。

    每个参数在全量交易上独立算 profit_impact()，不相互影响：
      - 交易不过滤，参数 A 的结果不影响参数 B
      - |impact| ≈ 0 → 无区分度，不输出（调用方保持当前值）
      - 有区分度 → 取值 = win_median（赚钱组参数值中位数）

    为什么不需要除以步长做归一化：
      每个参数在自己的量纲下算 |impact| 排序即可。
      排序只决定报告顺序，不影响最终取值。

    Args:
        trades: 近邻交易列表
        params_meta: 参数元数据，默认用模块级 _PARAMS_META

    Returns:
        dict: 有信号的参数 {k: val, ...}（全无信号时返回 {})
              调用方用 .get() 访问，缺参数意味着保持当前值
    """
    if params_meta is None:
        params_meta = _PARAMS_META

    result: dict[str, float] = {}

    for k, meta in params_meta.items():
        pi = profit_impact(trades, k)
        win_median = pi["win_median"]
        lose_median = pi["lose_median"]

        # NaN 传播防御：NaN 无意义，跳过
        if _np.isnan(win_median) or _np.isnan(lose_median):
            continue

        impact = pi["impact"]

        # |impact| ≈ 0 → 无区分度（不开枪原则 → 不输出）
        if abs(impact) < 1e-8:
            continue

        # snap 到网格后 win_median == lose_median → 无区分度
        snapped_win = _snap_to_grid(win_median, meta)
        snapped_lose = _snap_to_grid(lose_median, meta)
        if snapped_win == snapped_lose:
            continue

        # 有区分度：取值 = 赚钱组中位值（snap 后）
        result[k] = snapped_win

    # 日志按 |impact| 降序展示
    if result:
        sorted_items = sorted(result.items(),
                              key=lambda x: abs(
                                  profit_impact(trades, x[0])["impact"]),
                              reverse=True)
        log_str = ", ".join(f"{k}={v}" for k, v in sorted_items)
        _logger.info(f"打字机归因: 有信号参数={{{log_str}}}")
    else:
        _logger.info("打字机归因: 所有参数均无信号")

    return result


def _is_param_locked() -> bool:
    """检查参数是否被周优化锁定 — 已废弃（飞轮闭环后不再锁定）。"""
    return False


def configure_for_today(l0_score: float,
                        l0_trend: float = 50.0,
                        l0_volume: float = 50.0,
                        l0_breadth: float = 50.0) -> None:
    """每天开盘前执行一次。

    1. 在纸带上找最相似的 N 笔历史交易
    2. 归因得出今天最优参数
    3. 全局切换（所有层自动生效）

    如果 ParamLock 锁定中（周优化后 3 天内），归因仍运行但只读不写，
    用于漂移监控。

    可单独传 l0_score（兼容旧代码），此时其他维度默认 50。
    """
    global EXIT, RISK, ENTRY, MARKET

    if _TRADE_PAPER is None:
        _logger.warning(f"打字机归因跳过: 纸带未加载（调用 load_paper_tape() 加载）")
        return
    if len(_TRADE_PAPER) < 10:
        _logger.warning(f"打字机归因跳过: 纸带仅 {len(_TRADE_PAPER)} 笔（需要 >= 10 笔）")
        return

    today_fp = [l0_score, l0_trend, l0_volume, l0_breadth]
    _logger.info(f"打字机归因: L0={l0_score:.1f} 趋势={l0_trend:.1f} 量能={l0_volume:.1f} 宽度={l0_breadth:.1f}")

    neighbors = _find_neighbors(today_fp)
    if len(neighbors) < 10:
        _logger.warning(f"打字机归因跳过: 近邻仅 {len(neighbors)} 笔（需要 >= 10 笔）")
        return

    today_params = _attribution(neighbors)
    if not today_params:
        _logger.info("打字机归因: 所有参数均无信号...保持现有参数")
        return

    # ⚠️ ParamLock 已废弃 — 飞轮闭环后归因始终持久化
    # 直接写入全局参数（同时持久化到磁盘，防止进程退出丢失）
    n_updated = 0
    n_updated += _update_if_exists(EXIT.__dict__,   today_params, EXIT,   "EXIT")
    n_updated += _update_if_exists(RISK.__dict__,   today_params, RISK,   "RISK")
    n_updated += _update_if_exists(ENTRY.__dict__,  today_params, ENTRY,  "ENTRY")
    n_updated += _update_if_exists(MARKET.__dict__, today_params, MARKET, "MARKET")
    if n_updated:
        save_effective_params()
    _logger.info(f"打字机归因: {n_updated} 个参数已更新")


def _update_if_exists(config_dict: dict, params: dict,
                      config_obj: object, config_name: str) -> int:
    """如果 params 中有 config_dict 里存在的键，则覆盖全局参数。

    Returns: 更新参数个数。
    """
    global EXIT, RISK, ENTRY, MARKET
    overlap = {k: v for k, v in params.items() if k in config_dict}
    if not overlap:
        return 0

    mapping = {"EXIT": "EXIT", "RISK": "RISK", "ENTRY": "ENTRY", "MARKET": "MARKET"}
    cls_map = {"EXIT": ExitConfig, "RISK": RiskConfig,
               "ENTRY": EntryConfig, "MARKET": MarketConfig}

    cls = cls_map[config_name]
    merged = {**config_dict, **overlap}
    new_obj = cls(**merged)

    if config_name == "EXIT":
        EXIT = new_obj
    elif config_name == "RISK":
        RISK = new_obj
    elif config_name == "ENTRY":
        ENTRY = new_obj
    elif config_name == "MARKET":
        MARKET = new_obj

    return len(overlap)