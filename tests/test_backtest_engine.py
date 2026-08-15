from copy import deepcopy

import pytest

import backtest_engine
from backtest_engine import run_backtest


def make_bars(count=30, opens=None, closes=None):
    opens = opens or [100 + index for index in range(count)]
    closes = closes or list(opens)
    return [
        {
            "trade_date": f"202601{index + 1:02d}",
            "open_price": opens[index],
            "high_price": max(opens[index], closes[index]) + 1,
            "low_price": min(opens[index], closes[index]) - 1,
            "close_price": closes[index],
            "volume": 1_000 + index,
        }
        for index in range(count)
    ]


def install_deterministic_strategy(monkeypatch, signals, warmup_index=1, calls=None):
    def fake_indicators(bars):
        return [
            {
                "trade_date": bar["trade_date"],
                "sma_5": None if index < warmup_index else 101,
                "sma_20": None if index < warmup_index else 100,
                "rsi_14": None if index < warmup_index else 55,
                "volume_sma_20": None if index < warmup_index else 1_000,
                "volume_ratio": None if index < warmup_index else 1.1,
            }
            for index, bar in enumerate(bars)
        ]

    def fake_strategy(rows):
        if calls is not None:
            calls.append([row["trade_date"] for row in rows])
        date = rows[-1]["trade_date"]
        return {"signal": signals.get(date, "HOLD")}

    monkeypatch.setattr(backtest_engine, "calculate_technical_indicators", fake_indicators)
    monkeypatch.setattr(backtest_engine, "evaluate_lab_strategy_v1", fake_strategy)


def test_order_is_filled_at_next_day_open(monkeypatch):
    bars = make_bars(5, opens=[100, 110, 125, 140, 150])
    install_deterministic_strategy(
        monkeypatch,
        {bars[1]["trade_date"]: "BUY", bars[2]["trade_date"]: "SELL"},
    )

    result = run_backtest(bars)

    trade = result["closed_trades"][0]
    assert trade["entry_signal_date"] == bars[1]["trade_date"]
    assert trade["entry_date"] == bars[2]["trade_date"]
    assert trade["entry_price"] == 125
    assert trade["exit_signal_date"] == bars[2]["trade_date"]
    assert trade["exit_date"] == bars[3]["trade_date"]
    assert trade["exit_price"] == 140


def test_future_data_never_enters_past_signal_and_trade(monkeypatch):
    bars = make_bars(6)
    calls = []
    install_deterministic_strategy(
        monkeypatch,
        {bars[1]["trade_date"]: "BUY", bars[3]["trade_date"]: "SELL"},
        calls=calls,
    )
    baseline = run_backtest(bars[:5])
    baseline_calls = deepcopy(calls)

    calls.clear()
    changed = deepcopy(bars)
    changed[5]["open_price"] = 99_999
    changed[5]["close_price"] = 88_888
    extended = run_backtest(changed)

    assert baseline["closed_trades"] == extended["closed_trades"]
    assert [point["signal"] for point in baseline["equity_curve"]] == [
        point["signal"] for point in extended["equity_curve"][:4]
    ]
    assert baseline_calls == calls[: len(baseline_calls)]
    assert all(call[-1] == bars[index + 1]["trade_date"] for index, call in enumerate(calls))


def test_buy_hold_uses_same_evaluation_period(monkeypatch):
    bars = make_bars(4, opens=[50, 100, 200, 300], closes=[50, 100, 250, 400])
    install_deterministic_strategy(monkeypatch, {})

    result = run_backtest(bars, initial_cash=1_000)

    assert result["evaluation_start_date"] == bars[1]["trade_date"]
    assert result["evaluation_end_date"] == bars[-1]["trade_date"]
    assert result["buy_hold_return"] == pytest.approx(3.0)
    assert result["total_return"] == 0.0


def test_mdd_uses_daily_close_equity(monkeypatch):
    bars = make_bars(
        5,
        opens=[100, 100, 100, 100, 100],
        closes=[100, 100, 120, 90, 96],
    )
    install_deterministic_strategy(monkeypatch, {bars[1]["trade_date"]: "BUY"})

    result = run_backtest(bars, initial_cash=1_000)

    assert result["mdd"] == pytest.approx(0.25)
    assert [point["equity"] for point in result["equity_curve"]] == [1_000, 1_200, 900, 960]


def test_repeated_buy_does_not_add_to_position(monkeypatch):
    bars = make_bars(5, opens=[100] * 5)
    install_deterministic_strategy(
        monkeypatch,
        {bar["trade_date"]: "BUY" for bar in bars[1:4]},
    )

    result = run_backtest(bars, initial_cash=1_000)

    assert result["open_position"]["quantity"] == 10
    assert sum(point["executed_order"] == "BUY" for point in result["equity_curve"]) == 1


def test_sell_without_position_is_ignored(monkeypatch):
    bars = make_bars(4)
    install_deterministic_strategy(monkeypatch, {bars[1]["trade_date"]: "SELL"})

    result = run_backtest(bars)

    assert result["trade_count"] == 0
    assert result["open_position"] is None
    assert all(point["executed_order"] is None for point in result["equity_curve"])


def test_last_day_signal_is_not_executed(monkeypatch):
    bars = make_bars(4)
    install_deterministic_strategy(monkeypatch, {bars[-1]["trade_date"]: "BUY"})

    result = run_backtest(bars)

    assert result["open_position"] is None
    assert result["final_equity"] == 10_000_000


def test_open_position_is_marked_at_last_close_but_not_counted(monkeypatch):
    bars = make_bars(4, opens=[100, 100, 100, 100], closes=[100, 100, 100, 120])
    install_deterministic_strategy(monkeypatch, {bars[1]["trade_date"]: "BUY"})

    result = run_backtest(bars, initial_cash=1_000)

    assert result["final_equity"] == 1_200
    assert result["open_position"]["unrealized_profit"] == 200
    assert result["trade_count"] == 0
    assert result["wins"] == 0
    assert result["losses"] == 0
    assert result["win_rate"] is None


def test_closed_trade_statistics(monkeypatch):
    bars = make_bars(9, opens=[100, 100, 100, 110, 100, 100, 100, 90, 90])
    install_deterministic_strategy(
        monkeypatch,
        {
            bars[1]["trade_date"]: "BUY",
            bars[2]["trade_date"]: "SELL",
            bars[4]["trade_date"]: "BUY",
            bars[6]["trade_date"]: "SELL",
        },
    )

    result = run_backtest(bars, initial_cash=1_000)

    assert result["trade_count"] == 2
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["win_rate"] == 0.5


def test_unaffordable_buy_does_not_create_position(monkeypatch):
    bars = make_bars(4, opens=[100, 100, 2_000, 100])
    install_deterministic_strategy(monkeypatch, {bars[1]["trade_date"]: "BUY"})

    result = run_backtest(bars, initial_cash=1_000)

    assert result["open_position"] is None
    assert result["final_equity"] == 1_000


def test_real_indicator_warmup_is_separate_from_evaluation():
    bars = make_bars(30)

    result = run_backtest(bars)

    assert result["evaluation_start_date"] == bars[19]["trade_date"]
    assert result["evaluation_end_date"] == bars[-1]["trade_date"]
    assert len(result["equity_curve"]) == 11
    assert result["equity_curve"][0]["signal"] == "HOLD"


def test_insufficient_warmup_returns_neutral_result():
    result = run_backtest(make_bars(19))

    assert result["evaluation_start_date"] is None
    assert result["final_equity"] == 10_000_000
    assert result["equity_curve"] == []


def test_input_validation_and_input_is_not_mutated():
    bars = make_bars(20)
    original = deepcopy(bars)

    ordered = run_backtest(list(reversed(bars)))
    assert bars == original
    assert ordered["evaluation_start_date"] == bars[19]["trade_date"]

    with pytest.raises(ValueError):
        run_backtest([])
    with pytest.raises(ValueError):
        run_backtest([bars[0], bars[0]])
    with pytest.raises(ValueError):
        run_backtest([{key: value for key, value in bars[0].items() if key != "volume"}])
    invalid_price = deepcopy(bars)
    invalid_price[0]["open_price"] = 0
    with pytest.raises(ValueError):
        run_backtest(invalid_price)
    with pytest.raises(ValueError):
        run_backtest(bars, initial_cash=0)
