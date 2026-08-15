import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from lab_repository import SQLiteLabRepository
from trading_lab import LabStore, calculate_signal, search_stocks


client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def isolated_lab_store(tmp_path, monkeypatch):
    test_store = LabStore(SQLiteLabRepository(tmp_path / "api-test.db"))
    monkeypatch.setattr(main_module, "store", test_store)
    yield test_store


def fake_price(stock_code: str):
    prices = {"005930": "70000", "000660": "180000"}
    return {"output": {"stck_prpr": prices[stock_code]}}


def fake_balance():
    return {
        "output1": [{"tot_evlu_amt": "1000000", "tot_pchs_amt": "800000", "dnca_tot_amt": "200000"}],
        "output2": [{"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "2", "pchs_avg_pric": "60000", "evlu_amt": "140000"}],
    }


def test_stock_name_and_code_search():
    assert search_stocks("삼성")[0] == {"stock_code": "005930", "stock_name": "삼성전자"}
    assert search_stocks("000660")[0]["stock_name"] == "SK하이닉스"


def test_multiple_watchlist_prices(monkeypatch):
    monkeypatch.setattr(main_module, "get_current_price", fake_price)
    client.post("/api/watchlist", json={"stock_code": "005930"})
    client.post("/api/watchlist", json={"stock_code": "000660"})

    response = client.get("/api/watchlist")

    assert response.status_code == 200
    assert [item["current_price"] for item in response.json()["items"]] == [70000, 180000]


def test_signal_rules():
    condition = {"buy_below": 70000, "sell_above": 90000}
    assert calculate_signal(69000, condition) == "BUY"
    assert calculate_signal(95000, condition) == "SELL"
    assert calculate_signal(80000, condition) == "HOLD"
    assert calculate_signal(80000, None) == "HOLD"


def test_condition_api_changes_watchlist_signal(monkeypatch):
    monkeypatch.setattr(main_module, "get_current_price", fake_price)
    client.post("/api/watchlist", json={"stock_code": "005930"})

    response = client.post(
        "/api/conditions",
        json={"stock_code": "005930", "buy_below": 71000, "sell_above": 90000},
    )

    assert response.status_code == 200
    assert client.get("/api/watchlist").json()["items"][0]["signal"] == "BUY"


def test_mock_order_is_local_and_validated(monkeypatch):
    monkeypatch.setattr(main_module, "get_current_price", fake_price)

    response = client.post(
        "/api/mock-orders",
        json={"stock_code": "005930", "side": "buy", "quantity": 2},
    )

    assert response.status_code == 200
    assert response.json()["order"]["status"] == "mock_filled"
    assert response.json()["order"]["price"] == 70000
    assert client.get("/api/mock-orders").json()["orders"][0]["quantity"] == 2
    assert client.post("/api/mock-orders", json={"stock_code": "005930", "side": "buy", "quantity": 0, "price": 1}).status_code == 400


def test_dashboard_combines_balance_watchlist_and_orders(monkeypatch):
    monkeypatch.setattr(main_module, "get_current_price", fake_price)
    monkeypatch.setattr(main_module, "get_account_balance", fake_balance)
    client.post("/api/watchlist", json={"stock_code": "005930"})
    client.post("/api/mock-orders", json={"stock_code": "005930", "side": "sell", "quantity": 1, "price": 70000})

    response = client.get("/api/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["account"]["holdings"][0]["stock_name"] == "삼성전자"
    assert payload["watchlist"][0]["stock_code"] == "005930"
    assert payload["orders"][0]["side"] == "sell"
    assert client.get("/ui/dashboard").status_code == 200
