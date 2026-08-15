from pathlib import Path

from lab_repository import SQLiteLabRepository
from stock_master import normalize_stock_code


class LabStore:
    def __init__(self, repository: SQLiteLabRepository):
        self.repository = repository

    def clear(self):
        self.repository.clear()

    def add_watch(self, stock_code: str, stock_name: str):
        return self.repository.upsert_watch(stock_code, stock_name)

    def list_watch(self):
        return self.repository.list_watch()

    def set_condition(self, stock_code: str, buy_below: int | None, sell_above: int | None):
        if buy_below is None and sell_above is None:
            raise ValueError("매수 또는 매도 기준가가 필요합니다.")
        if buy_below is not None and buy_below <= 0:
            raise ValueError("매수 기준가는 양수여야 합니다.")
        if sell_above is not None and sell_above <= 0:
            raise ValueError("매도 기준가는 양수여야 합니다.")
        if not self.repository.watch_exists(stock_code):
            raise ValueError("관심종목을 먼저 등록해야 합니다.")
        return self.repository.upsert_condition(stock_code, buy_below, sell_above)

    def get_condition(self, stock_code: str):
        return self.repository.get_condition(stock_code)

    def add_order(self, stock_code: str, stock_name: str, side: str, quantity: int, price: int):
        if side not in {"buy", "sell"}:
            raise ValueError("매수 또는 매도만 가능합니다.")
        if quantity <= 0 or price <= 0:
            raise ValueError("수량과 가격은 양수여야 합니다.")
        return self.repository.insert_order(
            stock_code,
            stock_name,
            side,
            quantity,
            price,
            "mock_filled",
        )

    def list_orders(self):
        return self.repository.list_orders()

    def search_stocks(self, query: str, limit: int = 20):
        normalized_query = query.strip()
        if not normalized_query:
            return []
        return self.repository.search_stocks(normalized_query, limit)

    def stock_name(self, stock_code: str):
        normalized_code = normalize_stock_code(stock_code)
        stock = self.repository.get_stock(normalized_code)
        return stock["stock_name"] if stock else normalized_code


def search_stocks(query: str, limit: int = 20):
    return store.search_stocks(query, limit)


def stock_name(stock_code: str):
    return store.stock_name(stock_code)


def calculate_signal(current_price: int, condition: dict | None):
    if not condition:
        return "HOLD"
    buy_below = condition.get("buy_below")
    sell_above = condition.get("sell_above")
    if buy_below is not None and current_price <= buy_below:
        return "BUY"
    if sell_above is not None and current_price >= sell_above:
        return "SELL"
    return "HOLD"


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "trading_lab.db"
store = LabStore(SQLiteLabRepository(DEFAULT_DB_PATH))
