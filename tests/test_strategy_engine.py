from fastapi.testclient import TestClient

import app.main as main_module
from strategy_engine import evaluate_lab_strategy_v1


client = TestClient(main_module.app)


def row(date, sma5, sma20, close, rsi=55, volume_ratio=1.2):
    return {
        "trade_date": date,
        "sma_5": sma5,
        "sma_20": sma20,
        "close_price": close,
        "rsi_14": rsi,
        "volume_ratio": volume_ratio,
    }


def reason(result, rule_id):
    return next(item for item in result["reasons"] if item["rule_id"] == rule_id)


def test_buy_requires_actual_bullish_cross_and_all_filters():
    rows = [
        row("20260812", sma5=99, sma20=100, close=101),
        row("20260813", sma5=101, sma20=100, close=105, rsi=55, volume_ratio=1.2),
    ]

    result = evaluate_lab_strategy_v1(rows)

    assert result["signal"] == "BUY"
    assert result["sufficient_data"] is True
    assert reason(result, "buy_bullish_cross")["matched"] is True
    assert reason(result, "buy_close_above_sma20")["matched"] is True
    assert reason(result, "buy_rsi_range")["matched"] is True
    assert reason(result, "buy_volume_ratio")["matched"] is True
    assert reason(result, "buy_bullish_cross")["actual"]["previous_sma_5"] == 99


def test_buy_is_not_repeated_while_sma5_remains_above_sma20():
    rows = [
        row("20260812", sma5=99, sma20=100, close=101),
        row("20260813", sma5=101, sma20=100, close=105),
        row("20260814", sma5=103, sma20=101, close=106),
    ]

    assert evaluate_lab_strategy_v1(rows, target_index=1)["signal"] == "BUY"
    repeated = evaluate_lab_strategy_v1(rows, target_index=2)
    assert repeated["signal"] == "HOLD"
    assert reason(repeated, "buy_bullish_cross")["matched"] is False


def test_bearish_cross_is_sell_even_when_close_is_above_sma20():
    rows = [
        row("20260813", sma5=102, sma20=100, close=103),
        row("20260814", sma5=99, sma20=100, close=101),
    ]

    result = evaluate_lab_strategy_v1(rows)

    assert result["signal"] == "SELL"
    assert reason(result, "sell_bearish_cross")["matched"] is True
    assert reason(result, "sell_close_below_sma20")["matched"] is False


def test_close_below_sma20_is_sell_without_new_cross():
    rows = [
        row("20260813", sma5=98, sma20=100, close=101),
        row("20260814", sma5=97, sma20=100, close=99),
    ]

    result = evaluate_lab_strategy_v1(rows)

    assert result["signal"] == "SELL"
    assert reason(result, "sell_bearish_cross")["matched"] is False
    assert reason(result, "sell_close_below_sma20")["matched"] is True


def test_missing_previous_or_warmup_indicator_returns_hold():
    result = evaluate_lab_strategy_v1(
        [row("20260813", None, None, close=100, rsi=None, volume_ratio=None)]
    )

    assert result["signal"] == "HOLD"
    assert result["sufficient_data"] is False
    assert result["reasons"][0]["rule_id"] == "insufficient_data"


def test_failed_buy_filter_returns_hold_not_repeated_buy():
    rows = [
        row("20260812", sma5=99, sma20=100, close=101),
        row("20260813", sma5=101, sma20=100, close=105, rsi=70, volume_ratio=0.9),
    ]

    result = evaluate_lab_strategy_v1(rows)

    assert result["signal"] == "HOLD"
    assert reason(result, "buy_bullish_cross")["matched"] is True
    assert reason(result, "buy_rsi_range")["matched"] is False
    assert reason(result, "buy_volume_ratio")["matched"] is False


def test_strategy_api_returns_signal_and_reasons(monkeypatch):
    bars = [
        {"trade_date": "20260812", "close_price": 101},
        {"trade_date": "20260813", "close_price": 105},
    ]
    indicator_rows = [
        {"trade_date": "20260812", "sma_5": 99, "sma_20": 100, "rsi_14": 52, "volume_ratio": 1.1},
        {"trade_date": "20260813", "sma_5": 101, "sma_20": 100, "rsi_14": 55, "volume_ratio": 1.2},
    ]

    class FakeMarketDataService:
        def __init__(self, repository):
            pass

        def get_daily_prices(self, stock_code, days):
            return {"stock_code": stock_code, "items": bars}

    monkeypatch.setattr(main_module, "DailyMarketDataService", FakeMarketDataService)
    monkeypatch.setattr(main_module, "calculate_technical_indicators", lambda _: indicator_rows)

    response = client.get("/api/stocks/005930/strategy")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stock_code"] == "005930"
    assert payload["strategy_id"] == "lab_strategy_v1"
    assert payload["signal"] == "BUY"
    assert len(payload["reasons"]) == 6
