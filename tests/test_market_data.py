from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main_module
import kis_api
from lab_repository import SQLiteLabRepository
from market_data import DailyMarketDataService, normalize_daily_prices
from trading_lab import LabStore


client = TestClient(main_module.app)
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone(timedelta(hours=9)))


def fake_daily_response():
    return {
        "rt_cd": "0",
        "output2": [
            {
                "stck_bsop_date": "20260814",
                "stck_oprc": "71000",
                "stck_hgpr": "72000",
                "stck_lwpr": "70000",
                "stck_clpr": "71500",
                "acml_vol": "12000000",
                "acml_tr_pbmn": "850000000000",
            },
            {
                "stck_bsop_date": "20260813",
                "stck_oprc": "70000",
                "stck_hgpr": "71500",
                "stck_lwpr": "69500",
                "stck_clpr": "71000",
                "acml_vol": "11000000",
                "acml_tr_pbmn": "780000000000",
            },
        ],
    }


def test_kis_daily_request_uses_official_parameters(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return fake_daily_response()

    def fake_get(url, headers, params):
        captured.update(url=url, headers=headers, params=params)
        return FakeResponse()

    monkeypatch.setattr(kis_api, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(kis_api, "_kis_get", fake_get)

    kis_api.get_daily_item_chart_price("005930", "20260101", "20260815")

    assert captured["url"].endswith("/inquire-daily-itemchartprice")
    assert captured["headers"]["tr_id"] == "FHKST03010100"
    assert captured["params"]["FID_PERIOD_DIV_CODE"] == "D"
    assert captured["params"]["FID_ORG_ADJ_PRC"] == "0"
    assert captured["params"]["FID_INPUT_ISCD"] == "005930"


def test_daily_prices_are_numeric_and_sorted_ascending():
    items = normalize_daily_prices(fake_daily_response())

    assert [item["trade_date"] for item in items] == ["20260813", "20260814"]
    assert all(
        isinstance(item[field], int)
        for item in items
        for field in [
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "trading_value",
        ]
    )


def test_sqlite_upsert_updates_same_stock_and_date(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "prices.db")
    item = normalize_daily_prices(fake_daily_response())[0]
    repository.upsert_daily_prices("005930", [item], NOW.isoformat())
    updated = dict(item, close_price=99999)

    repository.upsert_daily_prices("005930", [updated], NOW.isoformat())

    rows = repository.get_daily_prices("005930", 100)
    assert len(rows) == 1
    assert rows[0]["close_price"] == 99999


def test_same_request_uses_cache_and_refresh_forces_fetch(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "cache.db")
    calls = []

    def fetcher(stock_code, start_date, end_date):
        calls.append((stock_code, start_date, end_date))
        return fake_daily_response()

    service = DailyMarketDataService(repository, fetcher=fetcher, now_provider=lambda: NOW)

    first = service.get_daily_prices("005930", days=2)
    second = service.get_daily_prices("005930", days=2)
    refreshed = service.get_daily_prices("005930", days=2, refresh=True)

    assert first["cached"] is False
    assert second["cached"] is True
    assert refreshed["cached"] is False
    assert len(calls) == 2


def test_cache_survives_repository_recreation(tmp_path):
    db_path = tmp_path / "restart.db"
    first_repository = SQLiteLabRepository(db_path)
    first_service = DailyMarketDataService(
        first_repository,
        fetcher=lambda *_: fake_daily_response(),
        now_provider=lambda: NOW,
    )
    first_service.get_daily_prices("005930", days=2)

    restarted_repository = SQLiteLabRepository(db_path)
    restarted_service = DailyMarketDataService(
        restarted_repository,
        fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("KIS 호출 금지")),
        now_provider=lambda: NOW,
    )

    result = restarted_service.get_daily_prices("005930", days=2)
    assert result["cached"] is True
    assert len(result["items"]) == 2


def test_daily_api_keeps_response_isolated(monkeypatch, tmp_path):
    repository = SQLiteLabRepository(tmp_path / "api.db")
    monkeypatch.setattr(main_module, "store", LabStore(repository))
    monkeypatch.setattr(kis_api, "get_daily_item_chart_price", lambda *_: fake_daily_response())

    response = client.get("/api/stocks/005930/daily?days=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stock_code"] == "005930"
    assert payload["period"] == "D"
    assert payload["adjusted"] is True
    assert len(payload["items"]) == 2
    assert "fetched_at" not in payload["items"][0]
    assert client.get("/api/stocks/005930/daily?days=101").status_code == 422
