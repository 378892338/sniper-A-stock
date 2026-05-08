"""量化系统入口"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config.settings import BACKTEST_START, BACKTEST_END, REBALANCE_FREQ
from data.downloader import DataDownloader
from backtest.run import BacktestEngine


def main():
    print("=" * 50)
    print("Quant System — 四层筛子策略回测")
    print("=" * 50)

    # 1. 获取股票池 + 数据（通过 DataDownloader → pre_filter → DataSource）
    print("\n[1/4] 获取股票列表...")
    downloader = DataDownloader()
    stock_list = downloader.fetch_stock_list()
    print(f"  经前置过滤后共 {len(stock_list)} 只股票")

    symbols = stock_list["symbol"].tolist()[:20]
    print(f"  测试池: {len(symbols)} 只（首批20只）")

    # 2. 获取行情数据
    print(f"\n[2/4] 获取行情数据 ({BACKTEST_START} ~ {BACKTEST_END})...")
    data_pack = downloader.fetch_full_data_pack(
        start=BACKTEST_START, end=BACKTEST_END,
        include_stocks=True, stock_symbols=symbols,
    )
    daily_data = data_pack.get("stock_data") or {}
    print(f"  获取到 {len(daily_data)} 只股票日线")

    if len(daily_data) < 5:
        print("  数据不足，退出")
        return

    # 3. 选一只验证因子计算
    print("\n[3/4] 验证因子计算...")
    from models.factors import calc_all_factors
    test_sym = list(daily_data.keys())[0]
    test_df = daily_data[test_sym]
    result = calc_all_factors(test_df, symbol=test_sym)
    last = result.iloc[-1]
    print(f"  {test_sym} 最新评分:")
    print(f"    技术面: {last.get('score_technical', 0):.1f}")
    print(f"    资金面: {last.get('score_capital', 0):.1f}")
    print(f"    基本面: {last.get('score_fundamental', 0):.1f}")
    print(f"    总分:   {last.get('score_total', 0):.1f}")

    # 4. 回测
    print(f"\n[4/4] 运行回测...")
    engine = BacktestEngine(
        start=BACKTEST_START,
        end=BACKTEST_END,
        freq=REBALANCE_FREQ,
        top_n=10,
    )
    result = engine.run(daily_data)
    engine.print_report(result)

    return result


if __name__ == "__main__":
    result = main()
