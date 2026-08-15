STRATEGY_ID = "lab_strategy_v1"


def _reason(rule_id: str, description: str, matched: bool, actual: dict):
    return {
        "rule_id": rule_id,
        "description": description,
        "matched": matched,
        "actual": actual,
    }


def evaluate_lab_strategy_v1(rows: list[dict], target_index: int = -1):
    if not rows:
        return _insufficient_result(None, None, None)
    index = target_index if target_index >= 0 else len(rows) - 1
    if index < 1 or index >= len(rows):
        current = rows[index] if 0 <= index < len(rows) else None
        return _insufficient_result(
            current.get("trade_date") if current else None,
            current,
            None,
        )

    previous = rows[index - 1]
    current = rows[index]
    required_values = [
        previous.get("sma_5"),
        previous.get("sma_20"),
        current.get("sma_5"),
        current.get("sma_20"),
        current.get("close_price"),
        current.get("rsi_14"),
        current.get("volume_ratio"),
    ]
    if any(value is None for value in required_values):
        return _insufficient_result(current.get("trade_date"), current, previous)

    bullish_cross = previous["sma_5"] <= previous["sma_20"] and current["sma_5"] > current["sma_20"]
    close_above_sma20 = current["close_price"] > current["sma_20"]
    rsi_in_range = 50 <= current["rsi_14"] < 70
    volume_confirmed = current["volume_ratio"] >= 1.0
    bearish_cross = previous["sma_5"] >= previous["sma_20"] and current["sma_5"] < current["sma_20"]
    close_below_sma20 = current["close_price"] < current["sma_20"]

    reasons = [
        _reason(
            "buy_bullish_cross",
            "SMA5가 SMA20을 상향 돌파",
            bullish_cross,
            {
                "previous_sma_5": previous["sma_5"],
                "previous_sma_20": previous["sma_20"],
                "current_sma_5": current["sma_5"],
                "current_sma_20": current["sma_20"],
            },
        ),
        _reason(
            "buy_close_above_sma20",
            "종가가 SMA20보다 높음",
            close_above_sma20,
            {"close_price": current["close_price"], "sma_20": current["sma_20"]},
        ),
        _reason(
            "buy_rsi_range",
            "RSI14가 50 이상 70 미만",
            rsi_in_range,
            {"rsi_14": current["rsi_14"], "minimum": 50, "maximum_exclusive": 70},
        ),
        _reason(
            "buy_volume_ratio",
            "Volume Ratio가 1.0 이상",
            volume_confirmed,
            {"volume_ratio": current["volume_ratio"], "minimum": 1.0},
        ),
        _reason(
            "sell_bearish_cross",
            "SMA5가 SMA20을 하향 돌파",
            bearish_cross,
            {
                "previous_sma_5": previous["sma_5"],
                "previous_sma_20": previous["sma_20"],
                "current_sma_5": current["sma_5"],
                "current_sma_20": current["sma_20"],
            },
        ),
        _reason(
            "sell_close_below_sma20",
            "종가가 SMA20보다 낮음",
            close_below_sma20,
            {"close_price": current["close_price"], "sma_20": current["sma_20"]},
        ),
    ]

    buy_matched = bullish_cross and close_above_sma20 and rsi_in_range and volume_confirmed
    sell_matched = bearish_cross or close_below_sma20
    signal = "BUY" if buy_matched else "SELL" if sell_matched else "HOLD"
    return {
        "strategy_id": STRATEGY_ID,
        "as_of_date": current["trade_date"],
        "signal": signal,
        "sufficient_data": True,
        "reasons": reasons,
    }


def _insufficient_result(as_of_date, current, previous):
    return {
        "strategy_id": STRATEGY_ID,
        "as_of_date": as_of_date,
        "signal": "HOLD",
        "sufficient_data": False,
        "reasons": [
            _reason(
                "insufficient_data",
                "전일·금일 Strategy 지표가 모두 필요함",
                False,
                {"previous": previous, "current": current},
            )
        ],
    }
