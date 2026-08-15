from dataclasses import asdict, dataclass
from threading import Lock


STOCK_CATALOG = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스",
    "005380": "현대차",
    "000270": "기아",
    "068270": "셀트리온",
    "035420": "NAVER",
    "035720": "카카오",
    "105560": "KB금융",
}


@dataclass
class WatchItem:
    stock_code: str
    stock_name: str


@dataclass
class Condition:
    stock_code: str
    buy_below: int | None = None
    sell_above: int | None = None


class LabStore:
    def __init__(self):
        self.watchlist: dict[str, WatchItem] = {}
        self.conditions: dict[str, Condition] = {}
        self.orders: list[dict] = []
        self._next_order_id = 1
        self._lock = Lock()

    def clear(self):
        with self._lock:
            self.watchlist.clear()
            self.conditions.clear()
            self.orders.clear()
            self._next_order_id = 1

    def add_watch(self, stock_code: str, stock_name: str):
        with self._lock:
            item = WatchItem(stock_code=stock_code, stock_name=stock_name)
            self.watchlist[stock_code] = item
            return asdict(item)

    def list_watch(self):
        with self._lock:
            return [asdict(item) for item in self.watchlist.values()]

    def set_condition(self, stock_code: str, buy_below: int | None, sell_above: int | None):
        if buy_below is None and sell_above is None:
            raise ValueError("매수 또는 매도 기준가가 필요합니다.")
        if buy_below is not None and buy_below <= 0:
            raise ValueError("매수 기준가는 양수여야 합니다.")
        if sell_above is not None and sell_above <= 0:
            raise ValueError("매도 기준가는 양수여야 합니다.")
        with self._lock:
            condition = Condition(stock_code, buy_below, sell_above)
            self.conditions[stock_code] = condition
            return asdict(condition)

    def get_condition(self, stock_code: str):
        with self._lock:
            condition = self.conditions.get(stock_code)
            return asdict(condition) if condition else None

    def add_order(self, stock_code: str, stock_name: str, side: str, quantity: int, price: int):
        if side not in {"buy", "sell"}:
            raise ValueError("매수 또는 매도만 가능합니다.")
        if quantity <= 0 or price <= 0:
            raise ValueError("수량과 가격은 양수여야 합니다.")
        with self._lock:
            order = {
                "id": self._next_order_id,
                "stock_code": stock_code,
                "stock_name": stock_name,
                "side": side,
                "quantity": quantity,
                "price": price,
                "status": "mock_filled",
            }
            self._next_order_id += 1
            self.orders.insert(0, order)
            return dict(order)

    def list_orders(self):
        with self._lock:
            return [dict(order) for order in self.orders]


def search_stocks(query: str, limit: int = 20):
    normalized = query.strip().lower()
    if not normalized:
        return []
    matches = [
        {"stock_code": code, "stock_name": name}
        for code, name in STOCK_CATALOG.items()
        if normalized in code.lower() or normalized in name.lower()
    ]
    return matches[:limit]


def stock_name(stock_code: str):
    return STOCK_CATALOG.get(stock_code, stock_code)


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


store = LabStore()
