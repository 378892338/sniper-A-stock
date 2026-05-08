"""数据源工厂 — 多源注册、自动选择"""

from data.interfaces import DataSource, FundFlowSource

_sources: dict[str, type[DataSource]] = {}
_fund_sources: dict[str, type[FundFlowSource]] = {}


def register_source(name: str, cls: type[DataSource]):
    """注册行情数据源"""
    _sources[name] = cls


def register_fundflow_source(name: str, cls: type[FundFlowSource]):
    """注册资金流数据源"""
    _fund_sources[name] = cls


def get_source(name: str | None = None) -> DataSource:
    """按名称获取数据源实例。name=None时读配置，name='auto'时自动选可用源"""
    if name is None or name == "auto":
        return _get_available_source()
    cls = _sources.get(name)
    if cls is None:
        available = list(_sources.keys())
        raise ValueError(f"未知数据源: {name}，可用: {available}")
    return cls()


def get_fundflow_source(name: str | None = None) -> FundFlowSource:
    """按名称获取资金流数据源实例"""
    if name is None or name == "auto":
        return _get_available_fundflow_source()
    cls = _fund_sources.get(name)
    if cls is None:
        available = list(_fund_sources.keys())
        raise ValueError(f"未知资金流数据源: {name}，可用: {available}")
    return cls()


def _get_available_source() -> DataSource:
    """自动选择第一个可用的行情数据源"""
    from config.settings import DATA_SOURCE_PREFERENCE
    from core.logger import get_logger
    from shared.retry import check_and_attempt_recovery
    logger = get_logger("data.sources")

    for name in DATA_SOURCE_PREFERENCE:
        cls = _sources.get(name)
        if cls is None:
            continue
        src = cls()
        if src.is_available() or check_and_attempt_recovery(src.ENDPOINT):
            return src
        logger.debug(f"数据源 {name} 不可用，尝试下一个")

    raise RuntimeError(f"所有数据源均不可用: {list(_sources.keys())}")


def _get_available_fundflow_source() -> FundFlowSource:
    """自动选择第一个可用的资金流数据源"""
    from config.settings import DATA_SOURCE_PREFERENCE
    from core.logger import get_logger
    from shared.retry import check_and_attempt_recovery
    logger = get_logger("data.sources")

    for name in DATA_SOURCE_PREFERENCE:
        cls = _fund_sources.get(name)
        if cls is None:
            continue
        src = cls()
        if src.is_available() or check_and_attempt_recovery(src.ENDPOINT):
            return src
        logger.debug(f"资金流数据源 {name} 不可用，尝试下一个")

    raise RuntimeError(f"所有资金流数据源均不可用: {list(_fund_sources.keys())}")


# 自动注册内置数据源
from data.sources.eastmoney import EastMoneyDataSource
register_source("eastmoney", EastMoneyDataSource)

from data.sources.sina import SinaDataSource
register_source("sina", SinaDataSource)

from data.sources.akshare import AkshareDataSource, AkshareFundFlowSource
register_source("akshare", AkshareDataSource)
register_fundflow_source("akshare", AkshareFundFlowSource)

# tushare 可选安装（需token）
try:
    from data.sources.tushare import TushareDataSource, TushareFundFlowSource
    register_source("tushare", TushareDataSource)
    register_fundflow_source("tushare", TushareFundFlowSource)
except ImportError:
    pass

# baostock 免费备选（无需token，作为降级后备）
try:
    from data.sources.baostock import BaostockDataSource
    register_source("baostock", BaostockDataSource)
except ImportError:
    pass
