
from candle_analysis import analyze_candles


def test_bullish_engulfing():
    candles = [
        {"open": 1.1050, "high": 1.1060, "low": 1.1040, "close": 1.1045},
        {"open": 1.1040, "high": 1.1070, "low": 1.1030, "close": 1.1065},
        {"open": 1.1060, "high": 1.1080, "low": 1.1050, "close": 1.1075},
    ]

    result = analyze_candles(candles)
    print("Test result:", result)


if __name__ == "__main__":
    test_bullish_engulfing()
