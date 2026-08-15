from indicators import calculate_technical_indicators
from strategy_engine import evaluate_lab_strategy_v1


INITIAL_CASH = 10_000_000
_REQUIRED_INDICATORS = ("sma_5", "sma_20", "rsi_14", "volume_ratio")


def run_backtest(bars: list[dict], initial_cash: int = INITIAL_CASH):
    """Run LAB Strategy v1 without any external I/O or real order calls."""
    ordered_bars = _validate_and_order_bars(bars)
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")

    evaluation_start_index = _find_evaluation_start(ordered_bars)
    if evaluation_start_index is None:
        return _empty_result(initial_cash)

    evaluation_bars = ordered_bars[evaluation_start_index:]
    evaluation_start_date = evaluation_bars[0]["trade_date"]
    evaluation_end_date = evaluation_bars[-1]["trade_date"]
    cash = initial_cash
    position = None
    pending_order = None
    closed_trades = []
    equity_curve = []
    running_peak = float(initial_cash)

    for index in range(evaluation_start_index, len(ordered_bars)):
        bar = ordered_bars[index]
        executed_order = None

        if pending_order == "BUY" and position is None:
            quantity = cash // bar["open_price"]
            if quantity > 0:
                cost = quantity * bar["open_price"]
                cash -= cost
                position = {
                    "entry_signal_date": ordered_bars[index - 1]["trade_date"],
                    "entry_date": bar["trade_date"],
                    "entry_price": bar["open_price"],
                    "quantity": quantity,
                }
                executed_order = "BUY"
        elif pending_order == "SELL" and position is not None:
            proceeds = position["quantity"] * bar["open_price"]
            cash += proceeds
            profit = (bar["open_price"] - position["entry_price"]) * position["quantity"]
            closed_trades.append(
                {
                    **position,
                    "exit_signal_date": ordered_bars[index - 1]["trade_date"],
                    "exit_date": bar["trade_date"],
                    "exit_price": bar["open_price"],
                    "profit": profit,
                    "return_rate": bar["open_price"] / position["entry_price"] - 1,
                    "result": "WIN" if profit > 0 else "LOSS" if profit < 0 else "EVEN",
                }
            )
            position = None
            executed_order = "SELL"
        pending_order = None

        # Recalculate from a prefix so no bar after T can affect T's signal.
        available_bars = ordered_bars[: index + 1]
        indicator_rows = calculate_technical_indicators(available_bars)
        strategy_rows = [
            {**indicator, "close_price": price["close_price"]}
            for price, indicator in zip(available_bars, indicator_rows)
        ]
        strategy_result = evaluate_lab_strategy_v1(strategy_rows)
        signal = strategy_result["signal"]

        if index < len(ordered_bars) - 1:
            if position is None and signal == "BUY":
                pending_order = "BUY"
            elif position is not None and signal == "SELL":
                pending_order = "SELL"

        quantity = position["quantity"] if position else 0
        equity = cash + quantity * bar["close_price"]
        running_peak = max(running_peak, equity)
        drawdown = equity / running_peak - 1
        equity_curve.append(
            {
                "trade_date": bar["trade_date"],
                "cash": cash,
                "position_quantity": quantity,
                "close_price": bar["close_price"],
                "equity": equity,
                "drawdown": drawdown,
                "signal": signal,
                "executed_order": executed_order,
            }
        )

    final_equity = equity_curve[-1]["equity"]
    wins = sum(trade["result"] == "WIN" for trade in closed_trades)
    losses = sum(trade["result"] == "LOSS" for trade in closed_trades)
    decided_trades = wins + losses
    last_close = evaluation_bars[-1]["close_price"]
    open_position = None
    if position is not None:
        open_position = {
            **position,
            "last_close": last_close,
            "unrealized_profit": (last_close - position["entry_price"]) * position["quantity"],
        }

    return {
        "evaluation_start_date": evaluation_start_date,
        "evaluation_end_date": evaluation_end_date,
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "total_return": final_equity / initial_cash - 1,
        "buy_hold_return": _buy_hold_return(evaluation_bars, initial_cash),
        "trade_count": len(closed_trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / decided_trades if decided_trades else None,
        "mdd": abs(min(point["drawdown"] for point in equity_curve)),
        "closed_trades": closed_trades,
        "open_position": open_position,
        "equity_curve": equity_curve,
    }


def _find_evaluation_start(bars: list[dict]):
    indicators = calculate_technical_indicators(bars)
    for index, row in enumerate(indicators):
        if all(row.get(field) is not None for field in _REQUIRED_INDICATORS):
            return index
    return None


def _buy_hold_return(evaluation_bars: list[dict], initial_cash: int):
    first_open = evaluation_bars[0]["open_price"]
    quantity = initial_cash // first_open
    remaining_cash = initial_cash - quantity * first_open
    final_value = remaining_cash + quantity * evaluation_bars[-1]["close_price"]
    return final_value / initial_cash - 1


def _validate_and_order_bars(bars: list[dict]):
    if not bars:
        raise ValueError("bars must not be empty")
    required = {"trade_date", "open_price", "close_price", "volume"}
    copied = []
    for bar in bars:
        missing = required - bar.keys()
        if missing:
            raise ValueError(f"bar is missing required fields: {sorted(missing)}")
        if not isinstance(bar["trade_date"], str) or len(bar["trade_date"]) != 8 or not bar["trade_date"].isdigit():
            raise ValueError("trade_date must be an 8-digit string")
        if not isinstance(bar["open_price"], (int, float)) or bar["open_price"] <= 0:
            raise ValueError("open_price must be positive")
        if not isinstance(bar["close_price"], (int, float)) or bar["close_price"] <= 0:
            raise ValueError("close_price must be positive")
        if not isinstance(bar["volume"], (int, float)) or bar["volume"] < 0:
            raise ValueError("volume must not be negative")
        copied.append(dict(bar))
    copied.sort(key=lambda item: item["trade_date"])
    dates = [bar["trade_date"] for bar in copied]
    if len(dates) != len(set(dates)):
        raise ValueError("trade_date must be unique")
    return copied


def _empty_result(initial_cash: int):
    return {
        "evaluation_start_date": None,
        "evaluation_end_date": None,
        "initial_cash": initial_cash,
        "final_equity": initial_cash,
        "total_return": 0.0,
        "buy_hold_return": 0.0,
        "trade_count": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": None,
        "mdd": 0.0,
        "closed_trades": [],
        "open_position": None,
        "equity_curve": [],
    }
