import sqlite3
from pathlib import Path

import psycopg

from config import settings
from lab_repository import SQLiteLabRepository
from operations import OperationsService


SOURCE = Path(__file__).resolve().parent / "data" / "trading_lab.db"
TABLES = [
    "stock_master",
    "watchlist",
    "trading_conditions",
    "daily_prices",
    "mock_orders",
    "operation_settings",
    "automation_universe",
    "quant_signals",
    "backtest_runs",
    "job_runs",
    "system_logs",
]
SERIAL_TABLES = ["mock_orders", "backtest_runs", "job_runs", "system_logs"]


def source_tables(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def main():
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    if not SOURCE.exists():
        raise RuntimeError(f"SQLite source does not exist: {SOURCE}")

    target_repository = SQLiteLabRepository(SOURCE, database_url=settings.DATABASE_URL)
    OperationsService(target_repository)

    with sqlite3.connect(SOURCE) as source, psycopg.connect(settings.DATABASE_URL) as target:
        source.row_factory = sqlite3.Row
        available = source_tables(source)
        target_total = sum(
            target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in TABLES
        )
        if target_total:
            raise RuntimeError("PostgreSQL target is not empty; migration stopped to protect data")

        target.execute("ALTER TABLE trading_conditions ALTER COLUMN buy_below TYPE BIGINT")
        target.execute("ALTER TABLE trading_conditions ALTER COLUMN sell_above TYPE BIGINT")
        target.execute("ALTER TABLE mock_orders ALTER COLUMN quantity TYPE BIGINT")
        target.execute("ALTER TABLE mock_orders ALTER COLUMN price TYPE BIGINT")
        for column in ("open_price", "high_price", "low_price", "close_price", "volume", "trading_value"):
            target.execute(f"ALTER TABLE daily_prices ALTER COLUMN {column} TYPE BIGINT")
        target.execute("ALTER TABLE backtest_runs ALTER COLUMN initial_cash TYPE BIGINT")

        counts = []
        for table in TABLES:
            if table not in available:
                counts.append((table, 0, 0))
                continue
            rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
            if rows:
                columns = rows[0].keys()
                names = ",".join(f'"{column}"' for column in columns)
                placeholders = ",".join(["%s"] * len(columns))
                with target.cursor() as cursor:
                    cursor.executemany(
                        f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                        [tuple(row[column] for column in columns) for row in rows],
                    )
            target_count = target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            counts.append((table, len(rows), target_count))

        for table in SERIAL_TABLES:
            target.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"EXISTS(SELECT 1 FROM {table}))"
            )

        mismatches = [item for item in counts if item[1] != item[2]]
        if mismatches:
            raise RuntimeError(f"Row count mismatch: {mismatches}")

    for table, source_count, target_count in counts:
        print(f"{table}: sqlite={source_count} postgresql={target_count}")
    print("MIGRATION_OK")


if __name__ == "__main__":
    main()
