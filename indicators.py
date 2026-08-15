def calculate_sma(values: list[int | float], period: int):
    if period <= 0:
        raise ValueError("period는 양수여야 합니다.")
    result = [None] * len(values)
    rolling_sum = 0.0
    for index, value in enumerate(values):
        rolling_sum += value
        if index >= period:
            rolling_sum -= values[index - period]
        if index >= period - 1:
            result[index] = rolling_sum / period
    return result


def calculate_rsi(values: list[int | float], period: int = 14):
    if period <= 0:
        raise ValueError("period는 양수여야 합니다.")
    result = [None] * len(values)
    if len(values) <= period:
        return result

    gains = []
    losses = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    result[period] = _rsi_value(average_gain, average_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
        result[index] = _rsi_value(average_gain, average_loss)
    return result


def _rsi_value(average_gain: float, average_loss: float):
    if average_gain == 0 and average_loss == 0:
        return 50.0
    if average_loss == 0:
        return 100.0
    if average_gain == 0:
        return 0.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def calculate_technical_indicators(bars: list[dict]):
    closes = [bar["close_price"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    sma_5 = calculate_sma(closes, 5)
    sma_20 = calculate_sma(closes, 20)
    rsi_14 = calculate_rsi(closes, 14)
    volume_sma_20 = calculate_sma(volumes, 20)

    result = []
    for index, bar in enumerate(bars):
        average_volume = volume_sma_20[index]
        volume_ratio = None
        if average_volume not in (None, 0):
            volume_ratio = volumes[index] / average_volume
        result.append(
            {
                "trade_date": bar["trade_date"],
                "sma_5": sma_5[index],
                "sma_20": sma_20[index],
                "rsi_14": rsi_14[index],
                "volume_sma_20": average_volume,
                "volume_ratio": volume_ratio,
            }
        )
    return result
