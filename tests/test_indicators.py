from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from indicators import calculate_rsi, calculate_sma, calculate_technical_indicators


client = TestClient(main_module.app)


def make_bars(count=100):
    return [
        {
            "trade_date": f"2026{index + 1:04d}",
            "open_price": 100 + index,
            "high_price": 102 + index,
            "low_price": 99 + index,
            "close_price": 101 + index,
            "volume": 1000 + index * 10,
            "trading_value": 100000 + index,
        }
        for index in range(count)
    ]


def test_sma_uses_exact_period_and_warmup_none():
    result = calculate_sma([1, 2, 3, 4, 5, 6], 5)

    assert result[:4] == [None, None, None, None]
    assert result[4:] == [3.0, 4.0]


def test_rsi_14_warmup_and_direction_boundaries():
    rising = calculate_rsi(list(range(1, 21)), 14)
    falling = calculate_rsi(list(range(21, 1, -1)), 14)
    flat = calculate_rsi([10] * 20, 14)

    assert rising[:14] == [None] * 14
    assert rising[14] == 100.0
    assert falling[14] == 0.0
    assert flat[14] == 50.0


def test_volume_sma_and_ratio_start_after_twenty_values():
    bars = make_bars(20)
    result = calculate_technical_indicators(bars)

    assert all(item["volume_sma_20"] is None for item in result[:19])
    expected_average = sum(bar["volume"] for bar in bars) / 20
    assert result[19]["volume_sma_20"] == expected_average
    assert result[19]["volume_ratio"] == pytest.approx(bars[19]["volume"] / expected_average)


def test_all_indicator_warmup_boundaries():
    result = calculate_technical_indicators(make_bars(25))

    assert result[3]["sma_5"] is None
    assert result[4]["sma_5"] is not None
    assert result[13]["rsi_14"] is None
    assert result[14]["rsi_14"] is not None
    assert result[18]["sma_20"] is None
    assert result[19]["sma_20"] is not None
    assert result[19]["volume_sma_20"] is not None


def test_indicator_calculation_does_not_mutate_ohlcv_input():
    bars = make_bars(25)
    original = deepcopy(bars)

    calculate_technical_indicators(bars)

    assert bars == original


def test_invalid_sma_and_rsi_periods_are_rejected():
    with pytest.raises(ValueError):
        calculate_sma([1, 2, 3], 0)
    with pytest.raises(ValueError):
        calculate_rsi([1, 2, 3], 0)


def test_indicators_api_returns_latest_and_recent_without_signal(monkeypatch):
    bars = make_bars(100)

    class FakeMarketDataService:
        def __init__(self, repository):
            pass

        def get_daily_prices(self, stock_code, days):
            assert days == 100
            return {"stock_code": stock_code, "items": bars}

    monkeypatch.setattr(main_module, "DailyMarketDataService", FakeMarketDataService)

    response = client.get("/api/stocks/005930/indicators?recent=5")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stock_code"] == "005930"
    assert payload["data_points"] == 100
    assert payload["as_of_date"] == bars[-1]["trade_date"]
    assert len(payload["items"]) == 5
    assert payload["latest"]["sma_5"] is not None
    assert payload["latest"]["sma_20"] is not None
    assert payload["latest"]["rsi_14"] is not None
    assert "signal" not in payload
