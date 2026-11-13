
from candle_analysis import analyze_candles


def generate_signal(candles, timeframe):
    """
    Generate a basic OTC signal from candle analysis.
    """

    if timeframe not in ("1m", "5m"):
        return {
            "signal": "WAIT",
            "timeframe": timeframe,
            "reason": "Unsupported timeframe"
        }

    result = analyze_candles(candles)

    return {
        "signal": result["signal"],
        "timeframe": timeframe,
        "reason": result["reason"]
    }
