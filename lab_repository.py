import sqlite3
from pathlib import Path


class SQLiteLabRepository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._create_tables()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_tables(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    stock_code TEXT PRIMARY KEY,
                    stock_name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS trading_conditions (
                    stock_code TEXT PRIMARY KEY,
                    buy_below INTEGER,
                    sell_above INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (stock_code) REFERENCES watchlist(stock_code)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS mock_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS stock_master (
                    stock_code TEXT PRIMARY KEY,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    refreshed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def replace_stock_master(self, stocks: list[dict]):
        if not stocks:
            raise ValueError("적재할 종목 마스터가 비어 있습니다.")
        rows = [
            (item["stock_code"], item["stock_name"], item["market"])
            for item in stocks
        ]
        with self._connect() as connection:
            connection.execute("DELETE FROM stock_master")
            connection.executemany(
                """
                INSERT INTO stock_master (stock_code, stock_name, market)
                VALUES (?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def count_stocks(self):
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM stock_master").fetchone()
        return row[0]

    def search_stocks(self, query: str, limit: int = 20):
        code_prefix = f"{query}%"
        name_prefix = f"{query}%"
        name_contains = f"%{query}%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code, stock_name
                FROM stock_master
                WHERE stock_code LIKE ?
                   OR stock_name LIKE ? COLLATE NOCASE
                ORDER BY CASE
                    WHEN stock_code = ? THEN 0
                    WHEN stock_code LIKE ? THEN 1
                    WHEN stock_name = ? COLLATE NOCASE THEN 2
                    WHEN stock_name LIKE ? COLLATE NOCASE THEN 3
                    ELSE 4
                END,
                stock_name COLLATE NOCASE,
                stock_code
                LIMIT ?
                """,
                (
                    code_prefix,
                    name_contains,
                    query,
                    code_prefix,
                    query,
                    name_prefix,
                    limit,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_stock(self, stock_code: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stock_code, stock_name FROM stock_master WHERE stock_code = ?",
                (stock_code,),
            ).fetchone()
        return dict(row) if row else None

    def clear(self):
        with self._connect() as connection:
            connection.execute("DELETE FROM trading_conditions")
            connection.execute("DELETE FROM watchlist")
            connection.execute("DELETE FROM mock_orders")
            connection.execute("DELETE FROM sqlite_sequence WHERE name = 'mock_orders'")

    def upsert_watch(self, stock_code: str, stock_name: str):
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watchlist (stock_code, stock_name)
                VALUES (?, ?)
                ON CONFLICT(stock_code) DO UPDATE SET stock_name = excluded.stock_name
                """,
                (stock_code, stock_name),
            )
        return {"stock_code": stock_code, "stock_name": stock_name}

    def list_watch(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT stock_code, stock_name FROM watchlist ORDER BY created_at, rowid"
            ).fetchall()
        return [dict(row) for row in rows]

    def watch_exists(self, stock_code: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM watchlist WHERE stock_code = ?",
                (stock_code,),
            ).fetchone()
        return row is not None

    def delete_watch(self, stock_code: str):
        with self._connect() as connection:
            connection.execute("DELETE FROM watchlist WHERE stock_code = ?", (stock_code,))

    def upsert_condition(self, stock_code: str, buy_below: int | None, sell_above: int | None):
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trading_conditions (stock_code, buy_below, sell_above)
                VALUES (?, ?, ?)
                ON CONFLICT(stock_code) DO UPDATE SET
                    buy_below = excluded.buy_below,
                    sell_above = excluded.sell_above,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (stock_code, buy_below, sell_above),
            )
        return {"stock_code": stock_code, "buy_below": buy_below, "sell_above": sell_above}

    def get_condition(self, stock_code: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stock_code, buy_below, sell_above FROM trading_conditions WHERE stock_code = ?",
                (stock_code,),
            ).fetchone()
        return dict(row) if row else None

    def insert_order(
        self,
        stock_code: str,
        stock_name: str,
        side: str,
        quantity: int,
        price: int,
        status: str,
    ):
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO mock_orders (stock_code, stock_name, side, quantity, price, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (stock_code, stock_name, side, quantity, price, status),
            )
            order_id = cursor.lastrowid
        return {
            "id": order_id,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "side": side,
            "quantity": quantity,
            "price": price,
            "status": status,
        }

    def list_orders(self):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, stock_code, stock_name, side, quantity, price, status
                FROM mock_orders
                ORDER BY id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]
