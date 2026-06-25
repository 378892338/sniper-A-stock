"""修复 volume 数据 — 清理重复行 + 从 Baostock 补回缺失 volume。

两步：
  1. 清理因日期格式不统一导致的重复行（删除 " 00:00:00" 后缀的重复记录）
  2. 对有缺失 volume 的股票，从 Baostock 批量重新获取并回填

用法: python scripts/fix_volume_data.py
"""

import sys, time, json, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import numpy as np

from data.local.warehouse import LocalDataWarehouse
from core.logger import get_logger

logger = get_logger("scripts.fix_volume_data")

MAX_WORKERS = 8
BATCH_SIZE = 50    # 每个子进程处理的股票数


# ── 第一步：清理重复行 ──────────────────────────────

def step1_clean_duplicates(wh: LocalDataWarehouse) -> int:
    """清理日期格式不统一导致的重复行。

    问题: 部分插入用 "2026-06-01 00:00:00"（astype(str) 带时间戳），
          部分用 "2026-06-01"（CSV 字符串），导致 INSERT OR IGNORE
          将同一 (symbol, date) 存为两行。

    处理: 删除带时间戳后缀的行（它们 volume 为 NaN）。
    """
    conn = wh._connect()
    try:
        # 找出所有带时间戳后缀的行
        cursor = conn.execute(
            "SELECT symbol, date FROM daily_bars WHERE date LIKE '% %'"
        )
        rows = cursor.fetchall()
        if not rows:
            logger.info("✅ 无重复行需要清理")
            return 0

        logger.info(f"发现 {len(rows)} 行带时间戳的记录")

        # 直接 DELETE 所有带时间戳的行
        cursor = conn.execute("DELETE FROM daily_bars WHERE date LIKE '% %'")
        deleted = cursor.rowcount
        conn.commit()
        logger.info(f"已清理 {deleted} 行重复数据")
        return deleted
    finally:
        conn.close()


# ── 第二步：缺失 volume 统计 ──────────────────────

def find_symbols_with_missing_volume(wh: LocalDataWarehouse) -> list[str]:
    """找出有缺失 volume 的股票及其缺失日期范围。"""
    conn = wh._connect()
    try:
        df = pd.read_sql(
            "SELECT symbol, COUNT(*) as total, "
            "SUM(CASE WHEN volume IS NULL OR volume = 0 THEN 1 ELSE 0 END) as missing "
            "FROM daily_bars GROUP BY symbol "
            "HAVING missing > 0 "
            "ORDER BY missing DESC",
            conn,
        )
        if df.empty:
            return []

        total_missing = df["missing"].sum()
        total_rows = df["total"].sum()
        logger.info(
            f"volume 缺失: {len(df)} 只股票, "
            f"{total_missing}/{total_rows} 行 ({total_missing/total_rows*100:.1f}%)"
        )

        # 只修复缺失率 > 1% 的股票（忽略零星缺失）
        df = df[df["missing"] / df["total"] > 0.01]
        symbols = df["symbol"].tolist()
        logger.info(f"需修复(缺失率>1%): {len(symbols)} 只股票")
        return symbols
    finally:
        conn.close()


# ── 第三步：Baostock volume 重获取 ─────────────────

def _sym_to_bs(symbol: str) -> str:
    return f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"


def _worker_main(symbols: list[str]) -> list[dict]:
    """子进程：用 Baostock 重新获取一批股票的 volume 数据。

    返回: [{"symbol": str, "date": str, "volume": float}, ...]
    """
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        logger.error(f"Baostock 登录失败: {lg.error_msg}")
        return []

    results = []
    try:
        for sym in symbols:
            try:
                rs = bs.query_history_k_data_plus(
                    _sym_to_bs(sym),
                    "date,volume",
                    start_date="2019-01-01",
                    end_date="2026-06-06",
                    frequency="d", adjustflag="2",
                )
                while (rs.error_code == "0") & rs.next():
                    row = rs.get_row_data()
                    dt = row[0].strip()
                    vol = float(row[1]) if row[1] and row[1] != "None" else 0.0
                    if vol > 0:
                        results.append({"symbol": sym, "date": dt, "volume": vol})
            except Exception:
                continue
    finally:
        bs.logout()

    return results


def step3_fill_volume(wh: LocalDataWarehouse, symbols: list[str]):
    """多进程用 Baostock 重新拉取 volume 并回填。"""
    if not symbols:
        logger.info("无缺失 volume 的股票")
        return

    logger.info(f"Baostock 多进程获取 volume: {len(symbols)} 只, {MAX_WORKERS} 进程")

    # 分块
    chunks = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    n = min(MAX_WORKERS, len(chunks))

    script = Path(__file__).resolve()
    procs = []
    for chunk in chunks:
        # 通过 JSON 传递参数
        args_json = json.dumps(chunk)
        proc = subprocess.Popen(
            [sys.executable, str(script), "--worker", args_json],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(Path(__file__).resolve().parent.parent),
        )
        procs.append(proc)

    # 收集结果
    all_updates: list[dict] = []
    for proc in procs:
        out, _ = proc.communicate()
        for line in reversed(out.strip().split("\n")):
            line = line.strip()
            if line.startswith("["):
                try:
                    batch = json.loads(line)
                    all_updates.extend(batch)
                except Exception:
                    pass
                break

    logger.info(f"Baostock 返回 {len(all_updates)} 行 volume 数据")

    # 回填到 SQLite — 先统计回填前缺失行数
    conn = wh._connect()
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM daily_bars WHERE volume IS NULL OR volume = 0"
        ).fetchone()[0]

        # 批量 UPDATE — 用临时表加速
        cur = conn.cursor()
        batch_data = [(r["volume"], r["symbol"], r["date"]) for r in all_updates]
        cur.executemany(
            "UPDATE daily_bars SET volume = ? WHERE symbol = ? AND date = ? "
            "AND (volume IS NULL OR volume = 0)",
            batch_data,
        )
        conn.commit()

        after = conn.execute(
            "SELECT COUNT(*) FROM daily_bars WHERE volume IS NULL OR volume = 0"
        ).fetchone()[0]
        filled = before - after
        logger.info(f"volume 回填: {filled} 行更新 (回填前 {before} → 回填后 {after})")
    finally:
        conn.close()


# ── 验证 ──────────────────────────────────────────

def verify(wh: LocalDataWarehouse):
    """校验 volume 修复效果。"""
    conn = wh._connect()
    try:
        df = pd.read_sql(
            "SELECT substr(date,1,4) as year, "
            "COUNT(*) as total, "
            "SUM(CASE WHEN volume IS NULL OR volume = 0 THEN 1 ELSE 0 END) as bad "
            "FROM daily_bars GROUP BY year ORDER BY year",
            conn,
        )
    finally:
        conn.close()

    print()
    print("=" * 50)
    print("  Volume 修复结果验证")
    print("=" * 50)
    for _, row in df.iterrows():
        bad_pct = row["bad"] / row["total"] * 100
        icon = "[OK]" if bad_pct < 1 else ("[WARN]" if bad_pct < 10 else "[FAIL]")
        print(f"  {row['year']}: {row['total']:>8} rows, volume missing {row['bad']:>6} ({bad_pct:>5.1f}%) {icon}")

    total_bad = df["bad"].sum()
    total_rows = df["total"].sum()
    overall = total_bad / total_rows * 100
    status = "[PASS]" if overall < 1 else ("[WARN]" if overall < 10 else "[FAIL]")
    print(f"  {'─' * 48}")
    print(f"  总计: {total_rows} 行, volume缺失 {total_bad} ({overall:.1f}%) {status}")
    print()

    from data.freshness import DataFreshnessChecker
    checker = DataFreshnessChecker()
    results = checker.verify_after_operation("precompute_l2", dates=["latest"])
    for r in results:
        print(f"  [{r['status'].upper()}] {r['check']}: {r['detail']}")


# ── 主函数 ──────────────────────────────────────────

def main():
    wh = LocalDataWarehouse()

    logger.info("=" * 50)
    logger.info("Step 1: 清理日期格式不统一导致的重复行")
    logger.info("=" * 50)
    step1_clean_duplicates(wh)

    logger.info("=" * 50)
    logger.info("Step 2: 统计缺失 volume 的股票")
    logger.info("=" * 50)
    symbols = find_symbols_with_missing_volume(wh)
    if not symbols:
        logger.info("✅ 无缺失 volume 的股票")
    else:
        logger.info("=" * 50)
        logger.info("Step 3: Baostock 批量回填 volume")
        logger.info("=" * 50)
        step3_fill_volume(wh, symbols)

    logger.info("=" * 50)
    logger.info("验证")
    logger.info("=" * 50)
    verify(wh)

    logger.info("✅ Volume 修复完成")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        idx = sys.argv.index("--worker")
        symbols = json.loads(sys.argv[idx + 1])
        result = _worker_main(symbols)
        print(json.dumps(result), flush=True)
    else:
        main()
