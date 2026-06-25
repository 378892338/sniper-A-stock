"""轻量子进程 Baostock 工作器 — 不加载 pandas/numpy，只写 CSV。

⚠️ DEPRECATED — Baostock 服务端已黑名单封禁，不再使用。
    个股日线更新改为 Fetcher 降级链（akshare_daily → akshare → sina → ...）。
    保留此文件仅用于参考，不会被任何代码调用。

每个进程仅依赖 Python 标准库 + baostock，内存占用约 12MB。
由 updater.py 通过 subprocess 启动，支持大批量并发。
"""
import argparse
import csv
import sys


def sym_to_baostock(symbol: str) -> str:
    return f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk", type=int, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    symbols = args.symbols.split(",")

    import baostock as bs
    import random
    import time

    # 启动随机抖动 0~3s，防止多个子进程同时 login（baostock 并发 login 会触发封禁）
    time.sleep(random.uniform(0, 3))

    lg = None
    for attempt in range(3):
        lg = bs.login()
        if lg.error_code == "0":
            break
        if attempt < 2:
            time.sleep(5)  # 登录失败等 5s 重试
    if lg is None or lg.error_code != "0":
        print(f"BSLOGIN_FAIL|{lg.error_msg}", flush=True)
        sys.exit(1)

    succeeded = 0
    total_rows = 0
    try:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "date", "open", "high", "low", "close", "volume", "amount"])
            for sym in symbols:
                try:
                    bs_code = sym_to_baostock(sym)
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount",
                        start_date=args.start, end_date=args.end,
                        frequency="d", adjustflag="2",
                    )
                    rows = 0
                    while (rs.error_code == "0") & rs.next():
                        row = rs.get_row_data()  # [date, open, high, low, close, volume, amount]
                        w.writerow([sym] + row)  # [symbol, date, open, high, low, close, volume, amount]
                        rows += 1
                    if rows > 0:
                        succeeded += 1
                        total_rows += rows
                except Exception:
                    continue
    finally:
        bs.logout()

    print(f"BSDONE|{args.chunk}|ok={succeeded}/{len(symbols)}|rows={total_rows}", flush=True)


if __name__ == "__main__":
    main()
