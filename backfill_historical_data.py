import argparse

from lab_repository import SQLiteLabRepository
from market_data import HistoricalMarketDataService
from trading_lab import DEFAULT_DB_PATH


def main():
    parser = argparse.ArgumentParser(description="Backfill KIS daily prices toward the past.")
    parser.add_argument("stock_code", help="KIS six-digit stock code")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--end-date", help="Target end date in YYYYMMDD format")
    args = parser.parse_args()

    repository = SQLiteLabRepository(DEFAULT_DB_PATH)
    result = HistoricalMarketDataService(repository).backfill(
        args.stock_code,
        years=args.years,
        target_end_date=args.end_date,
    )
    print(
        f"stock={result['stock_code']} total={result['total_count']} "
        f"oldest={result['oldest_trade_date']} latest={result['latest_trade_date']} "
        f"calls={result['api_call_count']} backfilled={result['backfilled_count']}"
    )


if __name__ == "__main__":
    main()
