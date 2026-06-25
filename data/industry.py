"""行业分类数据 — 申万行业 + 概念板块"""

from pathlib import Path

import pandas as pd

from data.sources import get_source
from shared.cache import read_cache, write_cache
from core.logger import get_logger
from config.settings import SW2_BACKUP_PATH

logger = get_logger("data.industry")

# ═══════════════════════════════════════════
# 申万行业分类体系（2021版）
# ═══════════════════════════════════════════

# 申万一级行业指数代码映射（共31个）
SW_INDEX_MAP = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁",
    "801050": "有色金属", "801080": "电子",      "801110": "家用电器",
    "801120": "食品饮料", "801130": "纺织服饰", "801140": "轻工制造",
    "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
    "801180": "房地产",   "801200": "商贸零售", "801210": "社服",
    "801230": "综合",     "801710": "建筑材料", "801720": "建筑装饰",
    "801730": "电力设备", "801740": "国防军工", "801750": "计算机",
    "801760": "传媒",     "801770": "通信",      "801780": "银行",
    "801790": "非银金融", "801880": "汽车",      "801890": "机械设备",
    "801950": "煤炭",     "801960": "石油石化", "801970": "环保",
    "801980": "美容护理",
}

# 申万二级行业 → 上级一级行业 映射（131个二级行业）
# key = 二级行业代码（含.SI后缀）, value = (二级行业名, 上级一级行业名)
SW_INDEX_MAP_L2: dict[str, tuple[str, str]] = {
    "801016.SI": ("种植业", "农林牧渔"),   "801015.SI": ("渔业", "农林牧渔"),
    "801011.SI": ("林业", "农林牧渔"),     "801014.SI": ("饲料", "农林牧渔"),
    "801012.SI": ("农产品加工", "农林牧渔"),"801017.SI": ("养殖业", "农林牧渔"),
    "801018.SI": ("动物保健", "农林牧渔"), "801019.SI": ("农业综合", "农林牧渔"),
    "801033.SI": ("化学原料", "基础化工"), "801034.SI": ("化学制品", "基础化工"),
    "801032.SI": ("化学纤维", "基础化工"), "801036.SI": ("橡胶", "基础化工"),
    "801037.SI": ("塑料", "基础化工"),     "801038.SI": ("农用化工", "基础化工"),
    "801039.SI": ("非金属材料", "基础化工"),
    "801043.SI": ("冶钢原料", "钢铁"),     "801044.SI": ("普钢", "钢铁"),
    "801045.SI": ("特钢", "钢铁"),
    "801051.SI": ("金属新材料", "有色金属"),"801055.SI": ("工业金属", "有色金属"),
    "801053.SI": ("贵金属", "有色金属"),   "801054.SI": ("小金属", "有色金属"),
    "801056.SI": ("能源金属", "有色金属"),
    "801081.SI": ("半导体", "电子"),       "801083.SI": ("元件", "电子"),
    "801084.SI": ("光学光电子", "电子"),   "801082.SI": ("其他电子", "电子"),
    "801085.SI": ("消费电子", "电子"),     "801086.SI": ("电子化学品", "电子"),
    "801093.SI": ("汽车零部件", "汽车"),   "801092.SI": ("乘用车", "汽车"),
    "801881.SI": ("摩托车", "汽车"),       "801095.SI": ("商用车", "汽车"),
    "801111.SI": ("白色家电", "家用电器"), "801112.SI": ("黑色家电", "家用电器"),
    "801113.SI": ("小家电", "家用电器"),   "801114.SI": ("照明设备", "家用电器"),
    "801115.SI": ("厨卫电器", "家用电器"), "801116.SI": ("家电零部件", "家用电器"),
    "801117.SI": ("其他家电", "家用电器"),
    "801124.SI": ("食品加工", "食品饮料"), "801125.SI": ("白酒", "食品饮料"),
    "801126.SI": ("非白酒", "食品饮料"),   "801127.SI": ("休闲食品", "食品饮料"),
    "801128.SI": ("调味品", "食品饮料"),   "801129.SI": ("食品饮料", "食品饮料"),
    "801131.SI": ("纺织制造", "纺织服饰"), "801132.SI": ("服装家纺", "纺织服饰"),
    "801133.SI": ("饰品", "纺织服饰"),
    "801143.SI": ("造纸", "轻工制造"),     "801141.SI": ("包装印刷", "轻工制造"),
    "801142.SI": ("家居用品", "轻工制造"), "801145.SI": ("其他轻工", "轻工制造"),
    "801151.SI": ("化学制药", "医药生物"), "801155.SI": ("中药", "医药生物"),
    "801152.SI": ("生物制品", "医药生物"), "801154.SI": ("医药商业", "医药生物"),
    "801153.SI": ("医疗器械", "医药生物"), "801156.SI": ("医疗服务", "医药生物"),
    "801161.SI": ("电力", "公用事业"),     "801163.SI": ("燃气", "公用事业"),
    "801178.SI": ("航运", "交通运输"),     "801179.SI": ("铁路公路", "交通运输"),
    "801991.SI": ("保险", "交通运输"),     "801992.SI": ("物流", "交通运输"),
    "801181.SI": ("房地产开发", "房地产"), "801183.SI": ("房地产服务", "房地产"),
    "801202.SI": ("贸易", "商贸零售"),     "801203.SI": ("一般零售", "商贸零售"),
    "801204.SI": ("专业连锁", "商贸零售"), "801206.SI": ("电商", "商贸零售"),
    "801207.SI": ("百货", "商贸零售"),
    "801216.SI": ("旅游", "社服"),         "801218.SI": ("专业服务", "社服"),
    "801219.SI": ("酒店餐饮", "社服"),     "801993.SI": ("教育", "社服"),
    "801994.SI": ("体育", "社服"),
    "801782.SI": ("国有大型银行", "银行"), "801783.SI": ("股份制银行", "银行"),
    "801784.SI": ("城商行", "银行"),       "801785.SI": ("农商行", "银行"),
    "801193.SI": ("证券", "非银金融"),     "801194.SI": ("保险", "非银金融"),
    "801191.SI": ("多元金融", "非银金融"),
    "801231.SI": ("综合", "综合"),
    "801711.SI": ("水泥", "建筑材料"),     "801712.SI": ("玻璃玻纤", "建筑材料"),
    "801713.SI": ("装修建材", "建筑材料"),
    "801721.SI": ("房屋建设", "建筑装饰"), "801722.SI": ("装修装饰", "建筑装饰"),
    "801723.SI": ("基建建设", "建筑装饰"), "801724.SI": ("专业工程", "建筑装饰"),
    "801726.SI": ("工程咨询", "建筑装饰"),
    "801731.SI": ("电机", "电力设备"),     "801733.SI": ("新能源设备", "电力设备"),
    "801735.SI": ("电线电缆", "电力设备"), "801736.SI": ("电网设备", "电力设备"),
    "801737.SI": ("电池", "电力设备"),     "801738.SI": ("其他电源", "电力设备"),
    "801072.SI": ("通用设备", "机械设备"), "801074.SI": ("专用设备", "机械设备"),
    "801076.SI": ("轨交设备", "机械设备"), "801077.SI": ("工程机械", "机械设备"),
    "801078.SI": ("自动化设备", "机械设备"),
    "801741.SI": ("航天装备", "国防军工"), "801742.SI": ("航空装备", "国防军工"),
    "801743.SI": ("地面兵装", "国防军工"), "801744.SI": ("航海装备", "国防军工"),
    "801745.SI": ("军工电子", "国防军工"),
    "801101.SI": ("计算机设备", "计算机"), "801103.SI": ("IT服务", "计算机"),
    "801104.SI": ("软件开发", "计算机"),
    "801764.SI": ("游戏", "传媒"),         "801765.SI": ("广告营销", "传媒"),
    "801766.SI": ("影视院线", "传媒"),     "801767.SI": ("数字媒体", "传媒"),
    "801769.SI": ("出版", "传媒"),         "801995.SI": ("广播电视", "传媒"),
    "801223.SI": ("通信服务", "通信"),     "801102.SI": ("通信设备", "通信"),
    "801951.SI": ("煤炭开采", "煤炭"),     "801952.SI": ("焦炭", "煤炭"),
    "801961.SI": ("油气开采", "石油石化"), "801962.SI": ("油服工程", "石油石化"),
    "801963.SI": ("炼化贸易", "石油石化"),
    "801971.SI": ("环保设备", "环保"),     "801972.SI": ("环境治理", "环保"),
    "801981.SI": ("个护用品", "美容护理"), "801982.SI": ("化妆品", "美容护理"),
    "801983.SI": ("医疗美容", "美容护理"),
}

# 东方财富行业名 → 申万一级行业名 映射
# 差异原因：EM 使用全称，SW 使用简称；或 EM 多了一层概念分类
EM_SW_NAME_MAP: dict[str, str] = {
    # 全称→简称
    "社会服务": "社服",
    # EM 概念板块（无 SW 对应）→ 映射到最接近的 SW 行业
    "美容护理": "美容护理",   # SW index 801980
    # EM 多出的分类（SW 分类中没有这些，用最接近的替代）
    "综合": "综合",           # SW index 801230
}

# 申万行业缓存 key
INDUSTRY_CACHE_KEY = "sw_industry_cons"
# 概念板块缓存 key
CONCEPT_CACHE_KEY = "sw_concept_cons"
# SW2 成分股缓存 key
SW2_INDUSTRY_CACHE_KEY = "sw2_industry_cons"
# SW2 最近获取日期缓存 key
SW2_FETCH_DATE_KEY = "sw2_fetch_date"


# ── SW2 成分股 持久化备份路径 ──
# 无 TTL，JQData 成功获取时自动覆盖。当 JQData 不可用（试用到期等）时作为兜底。
# SW2_BACKUP_PATH 从 config.settings 导入


def _save_sw2_backup(df: pd.DataFrame):
    """保存 SW2 成分股到持久化备份（无 TTL，覆盖写入）。"""
    SW2_BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(SW2_BACKUP_PATH, index=False)
    logger.info(f"SW2 持久化备份已保存: {SW2_BACKUP_PATH} ({len(df)} 条)")


def _load_sw2_backup() -> pd.DataFrame | None:
    """从持久化备份读取 SW2 成分股。备份损坏或不存在时返回 None。"""
    if not SW2_BACKUP_PATH.exists():
        return None
    try:
        df = pd.read_parquet(SW2_BACKUP_PATH)
        if df.empty:
            return None
        logger.info(f"SW2 从持久化备份恢复: {len(df)} 条, {df['industry_l2'].nunique()} 行业")
        return df
    except Exception as e:
        logger.warning(f"SW2 持久化备份读取失败: {e}")
        return None


def fetch_sw2_members_jqdata(date: str = "2026-03-16") -> pd.DataFrame:
    """使用 JQData 获取全 A 股申万二级行业分类。

    调用链：ensure_auth() → get_all_securities() → get_industry()
    一次全量调用获取全部约 5500 只股票的 SW 行业分类。

    依赖：
        - data.sources.jqdata.ensure_auth() — 每进程 auth 一次
        - data.sources.jqdata.get_sw_industry_map() — 实际获取

    Args:
        date: 查询日期 YYYY-MM-DD。
              免费试用账号数据范围为 2025-03-09 ~ 2026-03-16。
              默认使用试用期最后一天 2026-03-16（最新可用数据）。

    Returns:
        DataFrame [symbol, industry_l1, industry_l2]
    """
    from data.sources.jqdata import ensure_auth, get_sw_industry_map

    if not ensure_auth():
        logger.warning("JQData auth 失败，无法获取 SW2 行业分类")
        return pd.DataFrame()

    try:
        df = get_sw_industry_map(date=date)
        if df.empty:
            logger.warning("JQData get_sw_industry_map 返回空数据")
            return pd.DataFrame()

        # 转换为统一格式: [symbol, industry_l2, industry_l1]
        # 去掉 .XSHE/.XSHG 后缀，与下游 daily_bars 的 symbol 格式一致
        out = pd.DataFrame({
            "symbol": df["symbol"].str.replace(r"\.\w+$", "", regex=True),
            "industry_l2": df["industry_l2"],
            "industry_l1": df["industry_l1"],
        })
        out = out[out["industry_l2"].str.len() > 0]
        out = out.drop_duplicates(subset=["symbol", "industry_l2"])

        # 保存持久化备份
        _save_sw2_backup(out)

        logger.info(f"JQData SW2: {len(out)} 条, {out['industry_l2'].nunique()} 二级行业")
        return out

    except Exception as e:
        logger.error(f"JQData 获取 SW2 失败: {e}")
        return pd.DataFrame()


def get_sw_industry_path(code_l2: str) -> tuple[str, str] | None:
    """获取申万二级行业代码对应的（一级行业名, 二级行业名）。

    Args:
        code_l2: 二级行业代码，如 '801016.SI'

    Returns:
        (一级行业名, 二级行业名) 或 None
    """
    info = SW_INDEX_MAP_L2.get(code_l2)
    if info:
        return (info[1], info[0])
    return None


def get_l2_codes_by_l1(l1_name: str) -> list[str]:
    """获取一级行业名下的所有二级行业代码列表。"""
    return [code for code, (name, parent) in SW_INDEX_MAP_L2.items()
            if parent == l1_name]


def fetch_industry_members(source: str = "akshare") -> pd.DataFrame:
    """
    获取全A股申万行业分类（一级行业）。

    数据源优先级：
      ① 本地缓存（24h TTL） → 直接返回
      ② JQData（聚宽）→ 直接取 SW 一级行业（真 SW，优先）
      ③ akshare（东方财富行业板块，原始主源）→ 降级后备
      ④ 空 DataFrame

    返回: DataFrame with columns [symbol, name, industry]
           industry = 申万一级行业名（如"银行""食品饮料"）
    """
    cached = read_cache(INDUSTRY_CACHE_KEY, ttl_seconds=86400)  # 24h TTL
    if cached is not None and not cached.empty:
        return cached

    # ② JQData 优先（真 SW 行业分类）
    logger.info("行业分类: 尝试 JQData...")
    df = _fetch_industry_members_jqdata()
    if not df.empty:
        return df

    # ③ akshare 降级（东财行业板块，EM→SW 映射）
    logger.info("行业分类 JQData 失败，降级到 akshare...")
    ds = get_source(source)
    if ds.is_available():
        try:
            df = ds.fetch_industry_members()
            if df is not None and not df.empty:
                logger.info(f"行业分类(akshare): {len(df)} 条")
                write_cache(df, INDUSTRY_CACHE_KEY)
                return df
        except Exception as e:
            logger.warning(f"行业分类 akshare 失败: {e}")

    # ④ 所有数据源失败
    logger.error("行业分类获取失败：缓存/JQData/akshare 均不可用")
    return pd.DataFrame()


def _fetch_industry_members_jqdata() -> pd.DataFrame:
    """使用 JQData 获取申万一级行业分类。"""
    from data.sources.jqdata import ensure_auth, get_sw_industry_map

    if not ensure_auth():
        logger.warning("JQData auth 失败，无法获取行业分类")
        return pd.DataFrame()

    try:
        sw_df = get_sw_industry_map()
        if sw_df.empty:
            return pd.DataFrame()

        # 获取股票名称，用向量化 merge 替代逐行 loc
        import jqdatasdk as jq
        stocks = jq.get_all_securities(types=["stock"])

        # [symbol, industry_l1, industry_l2] → 合入股票名 → [symbol, name, industry]
        out = sw_df[["symbol", "industry_l1"]].copy()
        out = out.rename(columns={"industry_l1": "industry"})
        out = out[out["industry"].str.len() > 0]
        # 统一 regex 去后缀
        out["symbol"] = out["symbol"].str.replace(r"\.\w+$", "", regex=True)
        # 合并股票名（left join 保留行业全量）
        names = stocks["display_name"].rename("name").reset_index()
        names["index"] = names["index"].str.replace(r"\.\w+$", "", regex=True)
        out = out.merge(names, left_on="symbol", right_on="index", how="left")
        out = out[["symbol", "name", "industry"]]
        out = out.drop_duplicates(subset=["symbol"])
        out = out.sort_values("symbol").reset_index(drop=True)

        logger.info(f"行业分类(JQData): {len(out)} 条, {out['industry'].nunique()} 行业")
        write_cache(out, INDUSTRY_CACHE_KEY)
        return out

    except Exception as e:
        logger.error(f"行业分类 JQData 失败: {e}")
        return pd.DataFrame()


def fetch_concept_members(source: str = "akshare") -> pd.DataFrame:
    """
    获取全A股概念板块分类。

    返回: DataFrame with columns [symbol, name, concept]
    """
    cached = read_cache(CONCEPT_CACHE_KEY, ttl_seconds=86400)
    if cached is not None and not cached.empty:
        return cached

    ds = get_source(source)
    if not ds.is_available():
        logger.warning(f"数据源 {ds.name()} 不可用，无法获取概念板块")
        return pd.DataFrame()

    try:
        df = ds.fetch_concept_members()
        if df is None or df.empty:
            logger.warning(f"数据源 {ds.name()} 不支持概念板块数据")
            return pd.DataFrame()
        logger.info(f"概念板块分类: {len(df)} 条")
        write_cache(df, CONCEPT_CACHE_KEY)
        return df
    except Exception as e:
        logger.error(f"获取概念板块分类失败: {e}")
        return pd.DataFrame()


def build_symbol_industry_map(industry_df: pd.DataFrame | None = None
                              ) -> dict[str, str]:
    """
    构建 股票代码 → 申万行业 映射。

    返回: {symbol: industry_name}
    """
    if industry_df is None:
        industry_df = fetch_industry_members()
    if industry_df.empty:
        return {}

    if "symbol" not in industry_df.columns or "industry" not in industry_df.columns:
        logger.warning("行业数据缺少必要列")
        return {}

    return dict(zip(industry_df["symbol"], industry_df["industry"]))


def build_symbol_concepts_map(concept_df: pd.DataFrame | None = None
                              ) -> dict[str, list[str]]:
    """
    构建 股票代码 → 概念板块列表 映射。

    返回: {symbol: [concept1, concept2, ...]}
    """
    if concept_df is None:
        concept_df = fetch_concept_members()
    if concept_df.empty:
        return {}

    if "symbol" not in concept_df.columns or "concept" not in concept_df.columns:
        logger.warning("概念数据缺少必要列")
        return {}

    result: dict[str, list[str]] = {}
    for _, row in concept_df.iterrows():
        sym = row["symbol"]
        concept = row["concept"]
        if sym not in result:
            result[sym] = []
        result[sym].append(concept)

    return result


# ── SW2 行业分类 ──


# ── 反爬配置 ──

# UA 池：轮换降低被识别概率
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# 抓取配置
_MAX_RETRIES = 5                    # 函数级最大重试次数
_RETRY_BACKOFF = 2                  # 指数退避基数（秒）
_PER_REQUEST_DELAY_MIN = 0.5        # 每个行业请求最小延迟（秒）
_PER_REQUEST_DELAY_MAX = 1.5        # 每个行业请求最大延迟（秒）
_BATCH_SIZE = 20                    # 每批抓取行业数
_CONSECUTIVE_FAIL_PAUSE = 5         # 连续失败后暂停秒数
_CONSECUTIVE_FAIL_THRESHOLD = 5     # 连续失败阈值
_MIN_INDUSTRIES = 50                # 最低成功行业数（低于此触发重试）
_INTERNAL_RETRIES = 2               # 单个行业请求内部重试次数


def _get_sw2_batch() -> list[tuple[str, str]]:
    """从 akshare 获取 SW2 行业代码+名称列表，返回 [(code, name), ...]。"""
    import akshare as ak
    sw2_info = ak.sw_index_second_info()
    return list(zip(sw2_info.iloc[:, 0].tolist(), sw2_info.iloc[:, 1].tolist()))


def _get_sw2_batch_fallback() -> list[tuple[str, str]]:
    """akshare 解析失败时的备用方案：直接从本地 SW_INDEX_MAP_L2 构建。"""
    from data.industry import SW_INDEX_MAP_L2
    return [(code, name) for code, (name, _) in SW_INDEX_MAP_L2.items()]


# 模块级懒加载：SW2 行业批次列表（只在首次调用 _get_sw2_batch() 时填充）
_swp2_loaded = False
_sw2_batch: list[tuple[str, str]] = []


def _ensure_sw2_batch() -> list[tuple[str, str]]:
    """确保 _sw2_batch 已加载。"""
    global _swp2_loaded, _sw2_batch
    if not _swp2_loaded:
        try:
            _sw2_batch = _get_sw2_batch()
        except Exception as e:
            logger.warning(f"akshare sw_index_second_info() 失败 ({e})，使用 SW_INDEX_MAP_L2 备用数据源")
            _sw2_batch = _get_sw2_batch_fallback()
        _swp2_loaded = True
    return _sw2_batch


def fetch_sw2_members(max_workers: int = 8) -> pd.DataFrame:
    """获取全A股申万二级行业分类。

    数据源优先级（自动降级）：
      ① 本地缓存（24h TTL） → 直接返回，不消耗 JQData 配额
      ② JQData（聚宽）→ 写入缓存 + 持久化备份，返回
      ③ 持久化备份 parquet → 无 TTL 兜底，JQData 不可用时使用
      ④ 空 DataFrame → 管线降级（已有降级逻辑）

    JQData 注意事项：
      - 免费试用账号数据范围 2025-03-09 ~ 2026-03-16
      - auth 不跨进程持久化，每次 Python 调用都会重新 auth
      - 全市场约 5500 只股票，一次 get_industry() 调用即可
      - 每日额度约 10 万数据点，一次全市场约 5500 点

    Returns:
        DataFrame [symbol, industry_l2, industry_l1] — 全A股股票→SW2行业映射
    """
    # ① 缓存命中（24h TTL）
    cached = read_cache(SW2_INDUSTRY_CACHE_KEY, ttl_seconds=86400)
    if cached is not None and not cached.empty:
        logger.info(f"SW2 缓存命中: {len(cached)} 条, {cached['industry_l2'].nunique()} 行业")
        return cached

    # ② JQData 获取（主数据源）
    logger.info("SW2 缓存未命中，尝试 JQData...")
    df = fetch_sw2_members_jqdata()
    if not df.empty:
        # 写入缓存，供后续调用直接命中
        write_cache(df, SW2_INDUSTRY_CACHE_KEY)
        return df

    # ③ 持久化备份兜底
    logger.warning("JQData 获取失败，尝试从持久化备份恢复...")
    backup = _load_sw2_backup()
    if backup is not None and not backup.empty:
        logger.warning(f"SW2 从备份恢复（数据可能滞后）: {len(backup)} 条")
        # 也写入缓存，避免下次再走此路径
        write_cache(backup, SW2_INDUSTRY_CACHE_KEY)
        return backup

    # ④ 所有数据源失败
    logger.error("SW2 成分股获取失败：缓存/JQData/备份均不可用")
    return pd.DataFrame()


def _fetch_sw2_once(max_workers: int = 8) -> pd.DataFrame:
    """单次抓取 SW2 成分股（不带重试，供外层循环调用）。"""
    import time as _time
    batch_list = _ensure_sw2_batch()
    logger.info(f"SW2: {len(batch_list)} 个行业, 开始请求 legulegu 成分股...")

    all_results: list[pd.DataFrame] = []
    consecutive_fails = 0

    for batch_start in range(0, len(batch_list), _BATCH_SIZE):
        batch = batch_list[batch_start:batch_start + _BATCH_SIZE]
        batch_codes = [c for c, _ in batch]
        batch_names = [n for _, n in batch]

        logger.info(f"  抓取第 {batch_start // _BATCH_SIZE + 1} 批 ({len(batch)} 个行业)...")
        batch_results = _fetch_batch(batch_codes, batch_names, max_workers)
        for df in batch_results:
            if df is not None and not df.empty:
                all_results.append(df)
                consecutive_fails = 0
            else:
                consecutive_fails += 1
                if consecutive_fails >= _CONSECUTIVE_FAIL_THRESHOLD:
                    logger.warning(f"  连续 {consecutive_fails} 个行业抓取失败，暂停 {_CONSECUTIVE_FAIL_PAUSE}s...")
                    _time.sleep(_CONSECUTIVE_FAIL_PAUSE)
                    consecutive_fails = 0

        # 批次间延迟
        _time.sleep(2)

    if not all_results:
        logger.warning("SW2 成分股全部获取失败")
        return pd.DataFrame()

    full = pd.concat(all_results, ignore_index=True)
    full = full.drop_duplicates(subset=["symbol", "industry_l2"])
    full = full.sort_values("symbol").reset_index(drop=True)

    n_industries = full['industry_l2'].nunique()
    logger.info(f"SW2 成分股: {len(full)} 条, {n_industries} 行业")
    write_cache(full, SW2_INDUSTRY_CACHE_KEY)
    return full


def _fetch_batch(
    codes: list[str],
    names: list[str],
    max_workers: int = 8,
) -> list[pd.DataFrame | None]:
    """抓取一批行业的成分股。

    数据源: akshare.sw_index_third_cons()（申万研究院官网，通过 curl_cffi 访问）。
    列位置: 0=序号, 1=股票代码, 2=股票简称, 3=流通市值, 4=发布日期。
    每个行业请求内部重试 _INTERNAL_RETRIES 次（网络抖动兜底）。
    请求间随机延迟。
    """
    import akshare as _ak
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import random as _random
    import time as _time

    results: list[pd.DataFrame | None] = [None] * len(codes)

    def _fetch_one(code: str, name: str) -> pd.DataFrame | None:
        """单个行业请求，内部重试 _INTERNAL_RETRIES 次。"""
        # akshare.sw_index_third_cons 需要纯数字代码（去掉 .SI 后缀）
        symbol = code.replace(".SI", "").replace(".SZ", "").replace(".SH", "")
        for i in range(_INTERNAL_RETRIES):
            try:
                df = _ak.sw_index_third_cons(symbol=symbol)
                if df is None or df.empty:
                    logger.debug(f"    {code}({name}): 空数据")
                    return None
                # 列位置固定: 1=股票代码, 2=股票简称
                stock_col = df.columns[1]
                out = pd.DataFrame({
                    "symbol": df[stock_col].astype(str).str.replace(r"\.\w+$", "", regex=True),
                    "industry_l2": name,
                }).dropna(subset=["symbol"])
                out = out[out["symbol"].str.len() > 0]
                if out.empty:
                    return None
                return out
            except Exception as e:
                if i < _INTERNAL_RETRIES - 1:
                    wait = 2 ** (i + 1)
                    logger.debug(f"    {code}({name}) 失败，{wait}s 后重试 ({i+1}/{_INTERNAL_RETRIES}): {e}")
                    _time.sleep(wait)
                    continue
                logger.debug(f"    {code}({name}) 最终失败: {e}")
                return None
        return None

    with ThreadPoolExecutor(max_workers=min(max_workers, len(codes))) as pool:
        futures = {
            pool.submit(_fetch_one, code, name): idx
            for idx, (code, name) in enumerate(zip(codes, names))
        }
        for future in as_completed(futures):
            idx = futures[future]
            result = future.result()
            if result is not None and not result.empty:
                results[idx] = result
            # 请求间延迟（在线程外，避免影响并发）
            if idx < len(codes) - 1:
                _time.sleep(_random.uniform(_PER_REQUEST_DELAY_MIN, _PER_REQUEST_DELAY_MAX))

    return results


def get_symbol_classification(symbol: str,
                              industry_map: dict[str, str] | None = None,
                              concept_map: dict[str, list[str]] | None = None
                              ) -> tuple[str | None, list[str]]:
    """
    获取单个股票的行业和概念分类。

    返回: (industry_name, [concept_names])
    """
    if industry_map is None:
        industry_map = build_symbol_industry_map()
    if concept_map is None:
        concept_map = build_symbol_concepts_map()

    industry = industry_map.get(symbol)
    concepts = concept_map.get(symbol, [])
    return industry, concepts
