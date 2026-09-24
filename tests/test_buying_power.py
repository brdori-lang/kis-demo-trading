"""Buying power is read from a fake KIS response; these tests make no broker call."""
from fastapi.testclient import TestClient
import pytest

import app.aura_routes as routes
from app.main import app
import kis_api


def test_vts_buying_power_uses_no_credit_fields(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"rt_cd": "0", "output": {"ord_psbl_cash": "100000",
                    "nrcvb_buy_amt": "70000", "nrcvb_buy_qty": "1", "max_buy_qty": "9"}}

    def transport(url, headers, params):
        captured.update(url=url, headers=headers, params=params)
        return Response()

    monkeypatch.setattr(kis_api.settings, "KIS_ENV", "virtual")
    monkeypatch.setattr(kis_api.settings, "KIS_ACCOUNT_NO", "12345678-01")
    monkeypatch.setattr(kis_api, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(kis_api, "_kis_get", transport)
    result = kis_api.get_buying_power("005930", 70000)
    assert result == {"stock_code": "005930", "order_price": 70000,
                      "cash_available": 70000, "quantity_available": 1, "credit_excluded": True}
    assert captured["headers"]["tr_id"] == "VTTC8908R"
    assert captured["params"]["ORD_DVSN"] == "00"
    assert captured["params"]["ORD_UNPR"] == "70000"
    assert captured["url"].endswith("/uapi/domestic-stock/v1/trading/inquire-psbl-order")
    assert "12345678" not in str(result)


def test_incomplete_buying_power_response_fails_closed(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"rt_cd": "0", "output": {"ord_psbl_cash": "100000", "max_buy_qty": "9"}}

    monkeypatch.setattr(kis_api.settings, "KIS_ENV", "virtual")
    monkeypatch.setattr(kis_api.settings, "KIS_ACCOUNT_NO", "12345678-01")
    monkeypatch.setattr(kis_api, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(kis_api, "_kis_get", lambda *args: Response())
    with pytest.raises(ValueError, match="불완전"):
        kis_api.get_buying_power("005930", 70000)


def test_buying_power_route_requires_shared_read_key(monkeypatch):
    monkeypatch.setattr(routes.settings, "AURA_INTEGRATION_READ_KEY", "test-read-key")
    monkeypatch.setattr(routes, "get_buying_power", lambda code, price: {
        "stock_code": code, "order_price": price, "cash_available": 70000,
        "quantity_available": 1, "credit_excluded": True})
    client = TestClient(app)
    path = "/api/integrations/aura/buying-power?stock_code=005930&order_price=70000"
    assert client.get(path).status_code == 403
    assert client.get(path, headers={"X-Aura-Read-Key": "wrong"}).status_code == 403
    response = client.get(path, headers={"X-Aura-Read-Key": "test-read-key"})
    assert response.status_code == 200
    assert response.json()["quantity_available"] == 1
