import importlib

from fastapi.testclient import TestClient

import app.main as main_module


client = TestClient(main_module.app)


def test_current_price_route_still_works(monkeypatch):
    def fake_get_current_price(stock_code: str):
        return {"output": {"stck_prpr": "50000"}}

    monkeypatch.setattr(main_module, "get_current_price", fake_get_current_price)

    response = client.get("/api/price/005930")

    assert response.status_code == 200
    assert response.json()["stock_code"] == "005930"
    assert response.json()["current_price"] == "50000"


def test_balance_route_exists_and_returns_json(monkeypatch):
    def fake_get_account_balance():
        return {
            "output1": {
                "tot_evlu_amt": "3000000",
                "tot_pchs_amt": "2500000",
                "dnca_tot_amt": "500000",
            },
            "output2": [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "10",
                    "pchs_avg_pric": "70000",
                    "evlu_amt": "800000",
                }
            ],
        }

    monkeypatch.setattr(main_module, "get_account_balance", fake_get_account_balance, raising=False)

    response = client.get("/api/account/balance")

    assert response.status_code == 200
    payload = response.json()
    assert "total_evaluation_amount" in payload
    assert payload["holdings"][0]["stock_code"] == "005930"
    assert "account_no" not in payload


def test_virtual_balance_request_uses_official_headers(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output1": {}, "output2": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setattr("kis_api.get_access_token", lambda: "fake-token")
    monkeypatch.setattr("kis_api.settings.KIS_APP_KEY", "fake-app-key")
    monkeypatch.setattr("kis_api.settings.KIS_APP_SECRET", "fake-app-secret")
    monkeypatch.setattr("kis_api.settings.KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setattr("kis_api.settings.KIS_ENV", "virtual")
    monkeypatch.setattr("kis_api._kis_get", fake_get)

    result = importlib.import_module("kis_api").get_account_balance()

    assert result == {"output1": {}, "output2": []}
    assert captured["headers"]["tr_id"] == "VTTC8434R"
    assert "Content-Type" in captured["headers"]
    assert "Accept" in captured["headers"]
    assert "charset" in captured["headers"]
    assert captured["params"]["CTX_AREA_FK100"] == ""
    assert captured["params"]["CTX_AREA_NK100"] == ""


def test_balance_route_handles_real_kis_output_shape(monkeypatch):
    live_like_response = {
        "ctx_area_fk100": " " * 120,
        "ctx_area_nk100": " " * 120,
        "output2": [
            {
                "dnca_tot_amt": "500000000",
                "tot_evlu_amt": "500000000",
                "tot_pchs_amt": "500000000",
            }
        ],
        "output1": [
            {
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "hldg_qty": "10",
                "pchs_avg_pric": "70000",
                "evlu_amt": "800000",
                "evlu_pfls_amt": "100000",
                "evlu_pfls_rt": "14.29",
            }
        ],
    }

    monkeypatch.setattr(main_module, "get_account_balance", lambda: live_like_response)

    response = client.get("/api/account/balance")

    assert response.status_code == 200
    payload = response.json()
    assert payload["deposit_amount"] == "500000000"
    assert payload["total_evaluation_amount"] == "500000000"
    assert payload["holdings"][0]["stock_code"] == "005930"


def test_balance_route_hides_external_error(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_account_balance",
        lambda: (_ for _ in ()).throw(RuntimeError("sensitive upstream response")),
    )

    response = client.get("/api/account/balance")

    assert response.status_code == 502
    assert response.json() == {"detail": "잔고 조회에 실패했습니다."}
    assert "sensitive upstream response" not in response.text
