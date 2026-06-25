"""信号数据表 schema — 写入与 LocalDataWarehouse 相同的 SQLite 文件"""

# 数据库路径（与 LocalDataWarehouse 共享）
DB_FILE = "data/local/meta.db"

# ── 信号表 DDL ──

CREATE_NORTHBOUND_FLOW = """
CREATE TABLE IF NOT EXISTS northbound_flow (
    date    TEXT NOT NULL,
    time    TEXT NOT NULL,
    sh_net  REAL,
    sz_net  REAL,
    total   REAL,
    PRIMARY KEY (date, time)
);
"""

CREATE_FUND_FLOW = """
CREATE TABLE IF NOT EXISTS fund_flow (
    symbol       TEXT NOT NULL,
    date         TEXT NOT NULL,
    main_net     REAL,
    retail_net   REAL,
    super_large  REAL,
    large_net    REAL,
    medium_net   REAL,
    small_net    REAL,
    PRIMARY KEY (symbol, date)
);
"""

CREATE_DRAGON_TIGER = """
CREATE TABLE IF NOT EXISTS dragon_tiger (
    date             TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    reason           TEXT,
    net_buy          REAL,
    buy_amount       REAL,
    sell_amount      REAL,
    institution_buy  REAL,
    institution_sell REAL,
    turnover_rate    REAL,
    PRIMARY KEY (date, symbol)
);
"""

CREATE_INDUSTRY_COMPARE = """
CREATE TABLE IF NOT EXISTS industry_compare (
    date           TEXT NOT NULL,
    industry_name  TEXT NOT NULL,
    daily_change   REAL,
    volume_change  REAL,
    volume_ratio   REAL,
    breadth        REAL,
    leader_symbol  TEXT,
    leader_change  REAL,
    rank           INTEGER,
    stock_count    INTEGER,
    PRIMARY KEY (date, industry_name)
);
"""

CREATE_HOT_STOCKS = """
CREATE TABLE IF NOT EXISTS hot_stocks (
    date        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    reason_tags TEXT,
    PRIMARY KEY (date, symbol)
);
"""

CREATE_QUARTERLY_FINANCIALS = """
CREATE TABLE IF NOT EXISTS quarterly_financials (
    symbol           TEXT NOT NULL,
    report_date      TEXT NOT NULL,
    eps              REAL,
    roe              REAL,
    net_profit       REAL,
    net_profit_yoy   REAL,
    revenue          REAL,
    revenue_yoy      REAL,
    gross_margin     REAL,
    pe_ttm           REAL,
    pb               REAL,
    debt_to_assets   REAL,
    free_cash_flow   REAL,
    PRIMARY KEY (symbol, report_date)
);
"""

ALL_SIGNAL_DDLS = [
    CREATE_NORTHBOUND_FLOW,
    CREATE_FUND_FLOW,
    CREATE_DRAGON_TIGER,
    CREATE_INDUSTRY_COMPARE,
    CREATE_HOT_STOCKS,
    CREATE_QUARTERLY_FINANCIALS,
]

# ── 表名常量 ──
T_NORTHBOUND = "northbound_flow"
T_FUND_FLOW = "fund_flow"
T_DRAGON_TIGER = "dragon_tiger"
T_INDUSTRY_COMPARE = "industry_compare"
T_HOT_STOCKS = "hot_stocks"
T_QUARTERLY = "quarterly_financials"
