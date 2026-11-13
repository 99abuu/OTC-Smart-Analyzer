
def analyze_candles(candles):
    """
    Analyze OHLC candles and detect basic price action patterns.
    Each candle: open, high, low, close
    """

    if len(candles) < 3:
        return {
            "signal": "WAIT",
            "reason": "Not enough candles"
        }

    current = candles[-1]
    previous = candles[-2]

    body = abs(current["close"] - current["open"])
    candle_range = current["high"] - current["low"]

    if candle_range <= 0:
        return {"signal": "WAIT", "reason": "Invalid candle"}

    upper_wick = current["high"] - max(
        current["open"], current["close"]
    )
    lower_wick = min(
        current["open"], current["close"]
    ) - current["low"]

    # Bullish engulfing
    bullish_engulfing = (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )

    # Bearish engulfing
    bearish_engulfing = (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )

    # Rejection candles
    bullish_rejection = (
        lower_wick > body * 2
        and current["close"] > current["open"]
    )

    bearish_rejection = (
        upper_wick > body * 2
        and current["close"] < current["open"]
    )

    if bullish_engulfing or bullish_rejection:
        return {
            "signal": "CALL",
            "reason": "Bullish price action detected"
        }

    if bearish_engulfing or bearish_rejection:
        return {
            "signal": "PUT",
            "reason": "Bearish price action detected"
        }

    return {
        "signal": "WAIT",
        "reason": "No clear pattern"
    }
