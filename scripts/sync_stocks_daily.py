"""多进程补充个股日线缓存 — subprocess 方案。

双源：TX源(OHLC/amount) + baostock(volume)。
各子进程完全独立（独立 baostock 连接、独立内存），防止进程污染。
"""

import os
import sys
import json
import time
import random
import subprocess
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

os.environ["TQDM_DISABLE"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.logger import get_logger

logger = get_logger("scripts.sync_stocks_daily")

CACHE_DIR = Path(__file__).resolve().parent.parent / "data/raw/_cache/backtest"
CUTOFF = "2026-05-01"
FETCH_START = "2026-04-27"
FETCH_END = datetime.now().strftime("%Y-%m-%d")
PARQUET_COLS = ["open", "close", "high", "low", "amount", "volume", "symbol"]
WORKERS = 4


def _to_tx_code(symbol: str) -> str:
    return f"sz{symbol}" if symbol.startswith(("0", "3")) else f"sh{symbol}"


def _to_bs_code(symbol: str) -> str:
    return f"sz.{symbol}" if symbol.startswith(("0", "3")) else f"sh.{symbol}"


def _is_main_board(symbol: str) -> bool:
    return symbol.startswith(("00", "30", "60"))


# ── 子进程入口 ──────────────────────────────────────────


def _worker_main(symbols: list[str]):
    """子进程：独立 baostock 连接，处理一组股票。"""
    import akshare as ak
    import baostock as bs

    rng = random.Random(os.getpid() + int(time.time() * 1000) % 100000)

    lg = bs.login()
    if lg.error_code != "0":
        print(json.dumps({"ok": 0, "fail": len(symbols), "failed": symbols[:]}), flush=True)
        return

    ok, fail, failed_list, last_relogin = 0, 0, [], 0

    try:
        for i, sym in enumerate(symbols):
            cache_path = CACHE_DIR / f"stock_{sym}_daily.parquet"

            # 读缓存
            try:
                existing = pd.read_parquet(cache_path)
            except Exception:
                existing = pd.DataFrame()
            if not existing.empty:
                existing = existing[existing.index < CUTOFF]

            # TX 源 OHLC/amount
            code = _to_tx_code(sym)
            df = None
            try:
                raw = ak.stock_zh_a_hist_tx(
                    symbol=code,
                    start_date=FETCH_START.replace("-", ""),
                    end_date=FETCH_END.replace("-", ""),
                )
                if not raw.empty:
                    if "日期" in raw.columns:
                        raw = raw.rename(columns={
                            "日期": "date", "开盘": "open", "收盘": "close",
                            "最高": "high", "最低": "low", "成交额": "amount",
                        })
                    need = ["date", "open", "close", "high", "low", "amount"]
                    if all(c in raw.columns for c in need):
                        df = raw[need].copy()
                        df["date"] = pd.to_datetime(df["date"])
                        for c in ["open", "close", "high", "low", "amount"]:
                            df[c] = pd.to_numeric(df[c], errors="coerce")
                            coerced = df[c].isna().sum()
                            if coerced:
                                print(json.dumps({"warn": True, "sym": sym, "col": c, "na": int(coerced)}), flush=True)
                        df = df.set_index("date")
            except Exception as e:
                print(json.dumps({"warn": True, "sym": sym, "src": "tx", "err": str(e)[:60]}), flush=True)

            # TX 失败 -> yfinance 备份
            if df is None:
                try:
                    import yfinance as yf
                    tkr = yf.Ticker(f"{code}.ss" if code.startswith("sh") else f"{code}.sz")
                    yf_df = tkr.history(start=FETCH_START, end=FETCH_END)
                    if not yf_df.empty:
                        yf_df = yf_df.rename(columns={
                            "Open": "open", "Close": "close", "High": "high",
                            "Low": "low", "Volume": "volume"
                        })
                        df = yf_df[["open", "close", "high", "low"]].copy()
                        df.index = pd.to_datetime(df.index)
                        df.index.name = "date"
                        df = df.tz_localize(None)
                        # amount 用 close*volume 估算
                        df["amount"] = df["close"] * yf_df["volume"]
                        df["volume"] = yf_df["volume"]
                except Exception as e:
                    print(json.dumps({"warn": True, "sym": sym, "src": "yf", "err": str(e)[:60]}), flush=True)
                    df = None

            if df is None or df.empty:
                fail += 1
                failed_list.append(sym)
                continue

            # baostock volume（yfinance 已有 volume 则跳过）
            if "volume" not in df.columns:
                try:
                    rs = bs.query_history_k_data_plus(
                        _to_bs_code(sym), "date,volume",
                        start_date=FETCH_START, end_date=FETCH_END,
                        frequency="d", adjustflag="1",
                    )
                    rows = rs.get_data()
                    if rows is not None and len(rows) > 0:
                        vol = pd.DataFrame(rows, columns=["date", "volume"])
                        vol["date"] = pd.to_datetime(vol["date"])
                        vol["volume"] = pd.to_numeric(vol["volume"], errors="coerce")
                        df["volume"] = vol.set_index("date")["volume"].reindex(df.index)
                    else:
                        df["volume"] = np.nan
                except Exception as e:
                    print(json.dumps({"warn": True, "sym": sym, "src": "bs", "err": str(e)[:60]}), flush=True)
                    df["volume"] = np.nan

            df["symbol"] = sym
            df = df[PARQUET_COLS]

            combined = pd.concat([existing, df]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.to_parquet(cache_path)
            ok += 1

            # 随机延迟
            if i < len(symbols) - 1:
                time.sleep(rng.uniform(0.5, 1.5))

            # 每 200 只重登 baostock
            if (i + 1) % 200 == 0:
                try:
                    bs.logout()
                except Exception:
                    pass
                lg = bs.login()
                if lg.error_code != "0":
                    print(json.dumps({"warn": True, "sym": sym, "src": "bs_relogin", "err": lg.error_msg}), flush=True)

    finally:
        try:
            bs.logout()
        except Exception:
            pass

    print(json.dumps({"ok": ok, "fail": fail, "failed": failed_list[:20]}), flush=True)


# ── 主进程 ──────────────────────────────────────────


def classify_stocks() -> list[str]:
    """返回需修复的主板股票列表。"""
    fix = []
    for f in sorted(CACHE_DIR.glob("stock_*_daily.parquet")):
        try:
            df = pd.read_parquet(f)
            sym = f.stem.replace("stock_", "").replace("_daily", "")
            if not _is_main_board(sym):
                continue
            may = df[df.index >= CUTOFF]
            if not may.empty and may.notna().all(axis=None):
                continue
            fix.append(sym)
        except Exception:
            sym = f.stem.replace("stock_", "").replace("_daily", "")
            if _is_main_board(sym):
                fix.append(sym)
    return fix


def main():
    fix = classify_stocks()
    total = len(fix)
    logger.info(f"需处理: {total} 只主板股票 ({WORKERS} 进程)")
    if total == 0:
        return

    n = min(WORKERS, total)
    chunk_size = (total + n - 1) // n
    chunks = [fix[i:i+chunk_size] for i in range(0, total, chunk_size)]

    t0 = time.time()
    script = Path(__file__).resolve()

    procs = []
    for wid, chunk in enumerate(chunks):
        # 每个子进程接收 symbols 列表作为参数
        # 通过 --worker 标志 + JSON 编码参数传递
        args = ["--worker"] + chunk
        proc = subprocess.Popen(
            [sys.executable, str(script)] + args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(Path(__file__).resolve().parent.parent),
        )
        procs.append((wid, proc, len(chunk)))
        logger.info(f"进程 {wid+1}/{n} 启动: {len(chunk)} 只")

    # 收集结果
    total_ok, total_fail = 0, 0
    all_failed = []

    for wid, proc, size in procs:
        out, _ = proc.communicate()
        # 解析最后一行 JSON
        for line in reversed(out.strip().split("\n")):
            line = line.strip()
            if line.startswith("{"):
                try:
                    r = json.loads(line)
                    total_ok += r["ok"]
                    total_fail += r["fail"]
                    all_failed.extend(r.get("failed", []))
                    logger.info(f"进程 {wid+1}: {r['ok']} OK, {r['fail']} FAIL / {size}")
                except Exception:
                    logger.warning(f"进程 {wid+1} 输出解析失败")
                break

    elapsed = time.time() - t0
    logger.info(
        f"完成: {total_ok} OK, {total_fail} FAIL "
        f"(共 {total}, 耗时 {elapsed/60:.1f} 分钟)"
    )
    if all_failed:
        logger.warning(f"失败 {len(all_failed)} 只: {all_failed[:10]}{'...' if len(all_failed) > 10 else ''}")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        idx = sys.argv.index("--worker")
        _worker_main(sys.argv[idx + 1:])
    else:
        main()
