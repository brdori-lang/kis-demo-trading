import sqlite3

from lab_repository import SQLiteLabRepository
from trading_lab import LabStore


def make_store(db_path):
    return LabStore(SQLiteLabRepository(db_path))


def test_data_survives_store_recreation(tmp_path):
    db_path = tmp_path / "persistent.db"
    first_store = make_store(db_path)
    first_store.add_watch("005930", "삼성전자")
    first_store.set_condition("005930", 70000, 90000)
    first_order = first_store.add_order("005930", "삼성전자", "buy", 2, 70000)

    restarted_store = make_store(db_path)

    assert restarted_store.list_watch() == [{"stock_code": "005930", "stock_name": "삼성전자"}]
    assert restarted_store.get_condition("005930") == {
        "stock_code": "005930",
        "buy_below": 70000,
        "sell_above": 90000,
    }
    assert restarted_store.list_orders() == [first_order]


def test_order_id_continues_after_store_recreation(tmp_path):
    db_path = tmp_path / "orders.db"
    first_store = make_store(db_path)
    first_store.add_order("005930", "삼성전자", "buy", 1, 70000)

    restarted_store = make_store(db_path)
    second_order = restarted_store.add_order("000660", "SK하이닉스", "sell", 1, 180000)

    assert second_order["id"] == 2


def test_condition_requires_watchlist_item(tmp_path):
    store = make_store(tmp_path / "condition.db")

    try:
        store.set_condition("005930", 70000, None)
    except ValueError as error:
        assert str(error) == "관심종목을 먼저 등록해야 합니다."
    else:
        raise AssertionError("관심종목이 없는 조건 저장은 실패해야 합니다.")


def test_deleting_watchlist_item_cascades_condition(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "cascade.db")
    store = LabStore(repository)
    store.add_watch("005930", "삼성전자")
    store.set_condition("005930", 70000, 90000)

    repository.delete_watch("005930")

    assert store.list_watch() == []
    assert store.get_condition("005930") is None


def test_database_contains_only_lab_tables_and_fields(tmp_path):
    db_path = tmp_path / "schema.db"
    make_store(db_path)

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        columns = {
            row[1]
            for table in tables
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    assert tables == {
        "watchlist",
        "trading_conditions",
        "mock_orders",
        "stock_master",
        "daily_prices",
    }
    assert not {"account_no", "app_key", "app_secret", "access_token"} & columns
