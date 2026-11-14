
def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            (value * multiplier)
            + (result * (1 - multiplier))
        )

    return result


def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "CALL"

    if candle["close"] < candle["open"]:
        return "PUT"

    return None


def bullish_engulfing(previous, current):
    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(previous, current):
    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def bullish_rejection(candle):
    body = abs(candle["close"] - candle["open"])
    lower_wick = min(
        candle["open"], candle["close"]
    ) - candle["low"]

    total_range = candle["high"] - candle["low"]

    if total_range <= 0:
        return False

    return (
        lower_wick >= max(body * 1.5, total_range * 0.35)
        and candle["close"] > candle["low"] + total_range * 0.5
    )


def bearish_rejection(candle):
    body = abs(candle["close"] - candle["open"])
    upper_wick = candle["high"] - max(
        candle["open"], candle["close"]
    )

    total_range = candle["high"] - candle["low"]

    if total_range <= 0:
        return False

    return (
        upper_wick >= max(body * 1.5, total_range * 0.35)
        and candle["close"] < candle["low"] + total_range * 0.5
    )


def analyze_signal(candles_1m, candles_5m):
    if len(candles_1m) < 25 or len(candles_5m) < 25:
        return None

    required = ("open", "high", "low", "close")

    for candle in candles_1m[-25:] + candles_5m[-25:]:
        if not all(
            isinstance(candle.get(field), (int, float))
            for field in required
        ):
            return None

    # يجب تمرير الشموع المغلقة فقط إلى المحرك.
    closes_1m = [c["close"] for c in candles_1m]
    closes_5m = [c["close"] for c in candles_5m]

    fast_5m = ema(closes_5m, 9)
    slow_5m = ema(closes_5m, 21)

    if fast_5m is None or slow_5m is None:
        return None

    if fast_5m > slow_5m:
        trend = "CALL"
    elif fast_5m < slow_5m:
        trend = "PUT"
    else:
        return None

    previous = candles_1m[-2]
    current = candles_1m[-1]

    direction = candle_direction(current)

    if direction != trend:
        return None

    if trend == "CALL":
        pattern = (
            bullish_engulfing(previous, current)
            or bullish_rejection(current)
        )
    else:
        pattern = (
            bearish_engulfing(previous, current)
            or bearish_rejection(current)
        )

    if not pattern:
        return None

    # فلتر بسيط لتجنب الدخول بعد حركة دقيقة كبيرة جداً.
    recent_ranges = [
        c["high"] - c["low"]
        for c in candles_1m[-11:-1]
    ]

    average_range = sum(recent_ranges) / len(recent_ranges)

    current_range = current["high"] - current["low"]

    if average_range <= 0 or current_range > average_range * 2.5:
        return None

    return {
        "direction": trend,
        "symbol": None,
        "entry": "Next 1-minute candle",
        "expiry_minutes": 1,
        "entry_reference_price": current["close"],
        "trend_timeframe": "5m",
        "confirmation_timeframe": "1m",
        "pattern": (
            "Engulfing"
            if (
                bullish_engulfing(previous, current)
                or bearish_engulfing(previous, current)
            )
            else "Wick Rejection"
        ),
        "status": "DEMO_ONLY"
    }
