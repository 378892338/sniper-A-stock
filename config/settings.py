"""量化系统全局配置"""

import os
from pathlib import Path

# 加载 .env 文件（无需 python-dotenv）
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    with open(_env_file, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip().strip("\"'").strip()
                if _k and _v:
                    os.environ.setdefault(_k, _v)

# 路径中心 — 所有运行时数据走 QUANT_DATA_ROOT（数据仓）
from config.paths import (  # noqa: E402
    QUANT_DATA_ROOT, DATA_RAW_DIR, OUTPUT_DIR, META_DB_PATH,
    DATA_LOCAL_DIR, DATA_CACHE_DIR, LOG_DIR,
)

# ============ 数据源配置 ============
# "akshare" | "akshare_daily" | "tushare" | "auto" — auto 按 DATA_SOURCE_PREFERENCE 顺序选第一个可用的
DATA_SOURCE = "auto"
DATA_SOURCE_PREFERENCE = ["tencent", "10jqka", "sina", "akshare"]  # 2026-07-10 重排: tencent 第一(实时+volume, 仅当日); 10jqka (HexinDataSource) 第二(历史回看主力); jqdata 下架(试用过期), eastmoney 不进第一梯队(限流+fetcher bug)
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")

# JQData（聚宽）配置
JQDATA_USERNAME = os.getenv("JQDATA_USERNAME", "")
JQDATA_PASSWORD = os.getenv("JQDATA_PASSWORD", "")

# 路径（兼容旧名：ROOT/SW2_BACKUP_PATH/QLIB_DIR/MODEL_DIR/RESULT_DIR 仍可用）
ROOT = Path(__file__).parent.parent
DATA_DIR = DATA_RAW_DIR
SW2_BACKUP_PATH = DATA_DIR / "sw2_members_backup.parquet"
QLIB_DIR = QUANT_DATA_ROOT / "qlib_data"
OUTPUT_DIR = OUTPUT_DIR                              # 来自 config.paths（数据仓 outputs）
MODEL_DIR = OUTPUT_DIR / "models"
RESULT_DIR = OUTPUT_DIR / "results"

def _ensure_dirs():
    # 优先保证数据仓 + 代码仓必要子目录都存在
    for d in [DATA_DIR, QLIB_DIR, OUTPUT_DIR, MODEL_DIR, RESULT_DIR, DATA_LOCAL_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)

_ensure_dirs()

# 回测参数
BACKTEST_START = "2019-01-01"
BACKTEST_END = "2026-04-30"
REBALANCE_FREQ = "monthly"  # 月度调仓
BENCHMARK = "000905"        # 中证500

# 状态机参数
STATE_COOLING_WEEKS = 4  # 清仓后冷却周数

# 股票池
STOCK_POOL = "all_a"        # 全A股
EXCLUDE_ST = True
EXCLUDE_NEW_IPO = True      # 排除上市<180天新股
IPO_DAYS_MIN = 180

# 管道权重
PIPE_WEIGHTS = {
    "technical": 0.55,
    "capital":   0.30,
    "fundamental": 0.15,
}

# ============ 技术面管道参数 ============

# L1 趋势层
TREND_CONFIG = {
    "ma_fast": 20,
    "ma_slow": 60,
    "ma_year": 250,
    "must_pass": True,
}

# L2 结构层 — 6类形态
STRUCTURE_CONFIG = {
    # 平台内涨停
    "platform_limit_up": {
        "min_days": 60,           # 平台最少交易日
        "max_amplitude": 0.25,    # 最大振幅
        "limit_up_pct": 0.095,    # 主板涨停阈值(创业板/科创板按20%)
        "close_near_high": 0.02,  # close与high差距≤2%
        "vol_ratio": 2.0,         # 量是前20日均量的倍数
        "score": 15,
    },
    # 涨停平台突破
    "platform_breakout": {
        "min_gap_days": 45,       # 距平台内涨停的最少天数
        "breakout_pct": 0.03,     # 突破涨幅≥3%
        "vol_ratio": 1.5,         # 量比
        "score": 20,
    },
    # 中枢突破(缠论)
    "zhongshu_breakout": {
        "min_strokes": 3,         # 最少重叠笔数
        "body_ratio": 1.5,        # 突破K线实体 vs 前5日均值
        "score": 15,
    },
    # 底部反转
    "bottom_reversal": {
        "min_formation_days": 30,
        "double_bottom_gap": 20,  # 双底两低点最小间隔
        "double_bottom_tol": 0.03, # 双底两低点最大差异
        "score": 15,
    },
    # 旗形/三角突破
    "flag_triangle": {
        "min_flagpole_pct": 0.10, # 旗杆最小涨幅
        "score": 15,
    },
    # 均线粘合
    "ma_converge": {
        "ma_list": [5, 10, 20, 60],
        "converge_pct": 0.05,     # 粘合范围5%
        "min_converge_days": 15,
        "score": 20,
    },
}

# L3 时机层
TIMING_CONFIG = {
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "vol_expand_ratio": 1.3,     # 量放大 vs 前5日均量
}

# L4 共振层
RESONANCE_CONFIG = {
    "weekly_confirm": True,
    "weekly_trend_weight": 0.3,
}

# ============ 资金面管道参数 ============
CAPITAL_CONFIG = {
    "northbound": {"score": 25, "lookback": 20},
    "margin":     {"score": 20, "lookback": 20},
    "big_order":  {"score": 25, "lookback": 10},
    "turnover":   {"score": 15, "max_daily": 0.10},
    "volume_price": {"score": 15, "lookback": 10},
}

# ============ 基本面管道参数 ============
FUNDAMENTAL_CONFIG = {
    "analyst_coverage": {"score": 25},  # 低覆盖=高分(逆向)
    "inst_holding":     {"score": 25},  # 低机构持股=高分(逆向)
    "accruals":         {"score": 20},  # 低应计=高分(逆向)
    "idiosyncratic_vol": {"score": 15, "lookback": 60},  # 低特质波动=高分
    "short_reversal":   {"score": 15, "lookback": 5},    # 短期下跌=高分(逆向)
}

# ============ 三层统一评分架构 ============
SECTOR_INTEGRATION = {
    # 申万行业辅助交叉验证
    "enable_aux_cross_validation": True,

    # 缠论中枢检测扩展
    "enable_chanlun_l1": True,
    "enable_chanlun_l2": True,

    # L1 联动阈值: 市场状态 → L2 Gate 最少条件数
    "gate_l1_link_thresholds": {
        "bull": 3,
        "volatile": 2,
        "weak": 2,
        "bear": "skip_l2",
    },

    # 看空信号加权
    "bearish_signal_weights": {
        "weekly_top_divergence": 3,
        "weekly_death_cross": 2,
        "daily_death_cross": 1,
        "daily_dif_below_zero": 1,
    },
    "bearish_intercept_threshold": 4,  # 加权≥4 → 拦截

    # 各层评分权重
    "score_weights": {
        "l1": {"trend": 40, "volume": 30, "fund": 30},
        "l2": {"trend": 30, "alpha": 25, "volume": 25, "fund": 20},
        "l3": {"trend": 30, "alpha": 25, "volume": 25, "fund": 20},
    },

    # 成交量三部分法参数
    "volume_decay": 0.92,
    "volume_lookback": 20,

    # Alpha 多周期权重
    "alpha_period_weights": {"1w": 0.2, "4w": 0.4, "13w": 0.4},
}

# 盘中数据源：当前未使用配置选择，直接通过 MootdxRealtimeSource 调用
