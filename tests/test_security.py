import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from backtest_engine import run_backtest
from security import safe_data_path, validate_outbound_url


client = TestClient(app)


def test_requests_reject_unknown_fields_and_invalid_identifiers():
    assert client.post(
        "/api/watchlist", json={"stock_code": "005930", "unexpected": True}
    ).status_code == 422
    assert client.get("/api/stocks/not-a-code/daily").status_code == 422
    assert client.get("/api/stocks/search?q=" + "x" * 101).status_code == 422


def test_mock_order_rejects_unbounded_values():
    response = client.post(
        "/api/mock-orders",
        json={"stock_code": "005930", "side": "buy", "quantity": 1_000_001, "price": 1},
    )
    assert response.status_code == 422


def test_file_and_network_boundaries_are_fail_closed():
    with pytest.raises(ValueError, match="inside"):
        safe_data_path("../token.json")
    with pytest.raises(ValueError, match="allowed KIS"):
        validate_outbound_url("http://127.0.0.1/secrets")
    with pytest.raises(ValueError, match="allowed KIS"):
        validate_outbound_url("https://example.com/data")


def test_backtester_rejects_unbounded_or_non_integer_capital():
    with pytest.raises(ValueError, match="bounded integer"):
        run_backtest([], initial_cash=True)
    with pytest.raises(ValueError, match="bounded integer"):
        run_backtest([], initial_cash=1_000_000_000_001)
