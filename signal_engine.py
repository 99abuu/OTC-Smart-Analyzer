
def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
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

    if candle_direction(current) != trend:
        return None

    if trend == "CALL":
        engulfing = bullish_engulfing(previous, current)
        rejection = bullish_rejection(current)
    else:
        engulfing = bearish_engulfing(previous, current)
        rejection = bearish_rejection(current)

    if not engulfing and not rejection:
        return None

    recent_ranges = [
        c["high"] - c["low"]
        for c in candles_1m[-11:-1]
    ]

    average_range = sum(recent_ranges) / len(recent_ranges)
    current_range = current["high"] - current["low"]

    if average_range <= 0:
        return None

    if current_range > average_range * 2.5:
        return None

    return {
        "direction": trend,
        "entry_reference_price": current["close"],
        "pattern": "Engulfing" if engulfing else "Wick Rejection",
        "expiry_minutes": 1,
        "status": "DEMO_ONLY"
    }


# ذاكرة الإشارات التي سبق إرسالها
sent_signals = set()


def is_new_signal(symbol, signal):
    if signal is None:
        return False

    key = (
        symbol,
        signal["direction"],
        signal["pattern"],
        signal["entry_reference_price"]
    )

    if key in sent_signals:
        return False

    sent_signals.add(key)

    # منع نمو الذاكرة بلا حدود
    if len(sent_signals) > 1000:
        sent_signals.clear()
        sent_signals.add(key)

    return True
