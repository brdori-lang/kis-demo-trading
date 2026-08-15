from lab_repository import SQLiteLabRepository
from stock_master import download_all_stock_masters
from trading_lab import DEFAULT_DB_PATH


def main():
    stocks = download_all_stock_masters()
    repository = SQLiteLabRepository(DEFAULT_DB_PATH)
    count = repository.replace_stock_master(stocks)
    print(f"국내주식 종목 마스터 갱신 완료: {count}개")


if __name__ == "__main__":
    main()
