"""覆盖率验证脚本 — 检查策略用到的股票数据完整性

用法:
  python scripts/verify_coverage.py                            # 验证所有 active 股票
  python scripts/verify_coverage.py --symbols 000001,000002    # 验证指定股票
  python scripts/verify_coverage.py --format json               # JSON 输出
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
from datetime import datetime

from data.local.warehouse import LocalDataWarehouse
from core.logger import get_logger

logger = get_logger("scripts.verify_coverage")


def main():
    parser = argparse.ArgumentParser(description="数据覆盖率验证")
    parser.add_argument("--symbols", type=str, default="",
                        help="指定股票列表（逗号分隔），默认所有 active")
    parser.add_argument("--start", type=str, default="2000-01-01")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="输出格式")
    args = parser.parse_args()
    end = args.end or datetime.now().strftime("%Y-%m-%d")

    warehouse = LocalDataWarehouse()

    from data.quality import validate_symbols, generate_quality_section
    from shared.fetcher import Fetcher

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        stock_df = warehouse.get_stock_list(status="active")
        symbols = stock_df["symbol"].tolist() if not stock_df.empty else []

    fetcher = Fetcher()

    # 覆盖率检查
    results = validate_symbols(warehouse, symbols, start=args.start, end=end)
    ok = sum(1 for v in results.values() if v["ok"])
    total = len(results)

    coverage = ok / total * 100 if total > 0 else 0

    if args.format == "json":
        output = {
            "total": total,
            "ok": ok,
            "coverage_pct": round(coverage, 2),
            "missing": [sym for sym, v in results.items() if not v["ok"]],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  数据覆盖率验证")
        print(f"{'='*60}")
        print(f"  股票总数:     {total}")
        print(f"  数据完整:     {ok} ({coverage:.1f}%)")
        print(f"  存在问题:     {total - ok}")
        print(f"  区间:         {args.start} ~ {end}")
        print()

        missing = [(sym, v) for sym, v in results.items() if not v["ok"]]
        if missing:
            print(f"  ── 问题股票 ({len(missing)} 只) ──")
            for sym, info in missing[:20]:
                prob_str = ", ".join(info["problems"])
                print(f"    {sym}: {prob_str}")
            if len(missing) > 20:
                print(f"    ... 还有 {len(missing) - 20} 只")
        print()

        # 质量报告
        section = generate_quality_section(warehouse, fetcher, symbols,
                                           start=args.start, end=end)
        print(section)
        print()

    if coverage < 99:
        logger.warning(f"覆盖率 {coverage:.1f}% < 99%, 建议运行 fix_missing_data.py")
    else:
        logger.info(f"覆盖率 {coverage:.1f}% ✅")


if __name__ == "__main__":
    main()
