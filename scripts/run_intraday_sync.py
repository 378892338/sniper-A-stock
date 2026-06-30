"""12:00 盘中数据同步 — 仅用 mootdx TCP 拉取实时行情写入 intraday_snapshot

不生成日报、不触发 L2、不跑信号表。仅为 14:45 的初稿准备数据。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime
from data.local.warehouse import LocalDataWarehouse
from data.local.pipeline_journal import PipelineJournal
from data.sources.mootdx_realtime import MootdxRealtimeSource
from core.logger import get_logger

logger = get_logger("scripts.intraday_sync")

today = datetime.now().strftime("%Y-%m-%d")
logger.info(f"=== 12:00 盘中数据同步开始: {today} ===")

# 交易日判断
wh = LocalDataWarehouse()
if not wh.is_trading_day(today):
    logger.info(f"非交易日 {today}，跳过")
    sys.exit(0)

# 获取活跃股票列表
stock_df = wh.get_stock_list(status="active")
if stock_df.empty:
    logger.warning("无活跃股票")
    sys.exit(0)

symbols = stock_df["symbol"].tolist()
logger.info(f"活跃股票: {len(symbols)} 只")

# Journal 记录
journal = PipelineJournal()
run_id = journal.start_run("intraday_sync")
journal.log(run_id, "probe", "__pipeline__", "ok", detail={"symbols": len(symbols), "mode": "intraday_sync"}, mode="intraday_sync")

# mootdx TCP 实时行情（12:00 盘中）
source = MootdxRealtimeSource(batch_size=200, min_interval=10.0, heartbeat=True)

# 分批拉取
batch_size = 200
ok = fail = skip = 0
for i in range(0, len(symbols), batch_size):
    batch = symbols[i:i + batch_size]
    try:
        df = source.fetch_quotes(batch)
        if df is not None and not df.empty:
            # 组装 intraday_snapshot 格式
            snap = df.rename(columns={
                "price": "price", "open": "open", "high": "high", "low": "low",
                "last_close": "pre_close", "volume": "volume", "amount": "amount",
            })
            snap["date"] = today
            snap["time"] = datetime.now().strftime("%H:%M:%S.000")
            snap["source"] = "mootdx"
            wh.store_intraday_snapshot(snap)
            ok += len(snap)
            for _, row in snap.iterrows():
                journal.log(run_id, "write", row["symbol"], "ok", rows_count=1,
                           data_start=today, data_end=today, source="mootdx", mode="intraday_sync")
        else:
            fail += len(batch)
    except Exception as e:
        logger.warning(f"batch {i} fail: {e}")
        fail += len(batch)

logger.info(f"=== 同步完成: ok={ok} fail={fail} (共{len(symbols)}只) ===")
journal.log(run_id, "write", "__summary__", "ok",
           detail={"ok": ok, "fail": fail, "total": len(symbols)}, mode="intraday_sync")
