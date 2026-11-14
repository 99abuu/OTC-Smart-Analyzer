
import logging
from math import isfinite

logger = logging.getLogger(__name__)

MIN_CANDLES = 60
MIN_SCORE = 1

_sent = set()


def valid_candles(candles):
    if not isinstance(candles, list):
        return False

    if len(candles) < MIN_CANDLES:
        return False

    for candle in candles:
        try:
            values = [
                float(candle["open"]),
                float(candle["high"]),
                float(candle["low"]),
                float(candle["close"]),
            ]
            if not all(isfinite(v) for v in values):
                return False
            if values[2] > values[1] or values[2] > values[0]:
                return False
            if values[1] < values[0] or values[1] < values[3]:
                return False
        except (KeyError, TypeError, ValueError):
            return False

    return True


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(len(values) - period, len(values))
    ]

    gains = sum(max(change, 0) for change in changes) / period
    losses = sum(max(-change, 0) for change in changes) / period

    if losses == 0:
        return 100.0

    return 100 - (100 / (1 + gains / losses))


def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "CALL"
    if candle["close"] < candle["open"]:
        return "PUT"
    return None


def add_signal(signals, direction, name, reason):
    if direction not in ("CALL", "PUT"):
        return

    signals.append({
        "direction": direction,
        "name": name,
        "reason": reason,
    })


def analyze_signal(candles_1m, candles_5m, symbol=None):
    if not valid_candles(candles_1m):
        return None

    if not valid_candles(candles_5m):
        return None

    closes = [float(c["close"]) for c in candles_1m]
    opens = [float(c["open"]) for c in candles_1m]
    highs = [float(c["high"]) for c in candles_1m]
    lows = [float(c["low"]) for c in candles_1m]

    closes5 = [float(c["close"]) for c in candles_5m]

    price = closes[-1]
    previous_price = closes[-2]

    current = candles_1m[-1]
    previous = candles_1m[-2]

    signals = []

    # 1. EMA 50 / 200
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)

    if ema50 is not None and ema200 is not None:
        if ema50 > ema200 and price > ema50:
            add_signal(
                signals, "CALL", "EMA 50/200",
                "السعر فوق EMA50 وEMA50 فوق EMA200"
            )
        elif ema50 < ema200 and price < ema50:
            add_signal(
                signals, "PUT", "EMA 50/200",
                "السعر تحت EMA50 وEMA50 تحت EMA200"
            )

    # 2. RSI
    current_rsi = rsi(closes)

    if current_rsi is not None:
        if current_rsi <= 25:
            add_signal(
                signals, "CALL", "RSI Oversold",
                f"RSI وصل إلى {current_rsi:.1f} في منطقة تشبع البيع"
            )
        elif current_rsi >= 75:
            add_signal(
                signals, "PUT", "RSI Overbought",
                f"RSI وصل إلى {current_rsi:.1f} في منطقة تشبع الشراء"
            )

    # 3. Support rejection
    support = min(lows[-21:-1])
    resistance = max(highs[-21:-1])

    average_range = sum(
        highs[i] - lows[i] for i in range(-15, 0)
    ) / 15

    tolerance = max(average_range * 0.20, price * 0.00005)

    if (
        current["low"] <= support + tolerance
        and current["close"] > current["open"]
        and current["close"] > support
    ):
        add_signal(
            signals, "CALL", "Support Rejection",
            "ارتداد صاعد من منطقة الدعم"
        )

    # 4. Resistance rejection
    if (
        current["high"] >= resistance - tolerance
        and current["close"] < current["open"]
        and current["close"] < resistance
    ):
        add_signal(
            signals, "PUT", "Resistance Rejection",
            "رفض هابط من منطقة المقاومة"
        )

    # 5. Resistance breakout
    if previous_price <= resistance and price > resistance:
        add_signal(
            signals, "CALL", "Resistance Breakout",
            "إغلاق فوق المقاومة السابقة"
        )

    # 6. Support breakdown
    if previous_price >= support and price < support:
        add_signal(
            signals, "PUT", "Support Breakdown",
            "إغلاق تحت الدعم السابق"
        )

    # 7. Bullish engulfing
    if (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    ):
        add_signal(
            signals, "CALL", "Bullish Engulfing",
            "نموذج ابتلاع شرائي"
        )

    # 8. Bearish engulfing
    if (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    ):
        add_signal(
            signals, "PUT", "Bearish Engulfing",
            "نموذج ابتلاع بيعي"
        )

    # 9. Momentum
    if len(closes) >= 6:
        momentum = price - closes[-6]

        if momentum > average_range * 1.5:
            add_signal(
                signals, "CALL", "Price Momentum",
                "زخم سعري صاعد خلال آخر 5 شموع"
            )
        elif momentum < -average_range * 1.5:
            add_signal(
                signals, "PUT", "Price Momentum",
                "زخم سعري هابط خلال آخر 5 شموع"
            )

    # 10. EMA 9 / 21 crossover
    ema9_now = ema(closes, 9)
    ema21_now = ema(closes, 21)
    ema9_prev = ema(closes[:-1], 9)
    ema21_prev = ema(closes[:-1], 21)

    if all(v is not None for v in (
        ema9_now, ema21_now, ema9_prev, ema21_prev
    )):
        if ema9_prev <= ema21_prev and ema9_now > ema21_now:
            add_signal(
                signals, "CALL", "EMA 9/21 Cross",
                "تقاطع EMA9 أعلى EMA21"
            )
        elif ema9_prev >= ema21_prev and ema9_now < ema21_now:
            add_signal(
                signals, "PUT", "EMA 9/21 Cross",
                "تقاطع EMA9 أسفل EMA21"
            )

    # 11. Pin bar
    candle_range = float(current["high"]) - float(current["low"])
    body = abs(float(current["close"]) - float(current["open"]))

    if candle_range > 0:
        lower_wick = min(current["open"], current["close"]) - current["low"]
        upper_wick = current["high"] - max(current["open"], current["close"])

        if lower_wick >= candle_range * 0.60 and body <= candle_range * 0.30:
            add_signal(
                signals, "CALL", "Bullish Pin Bar",
                "ذيل سفلي طويل يشير إلى رفض الهبوط"
            )

        if upper_wick >= candle_range * 0.60 and body <= candle_range * 0.30:
            add_signal(
                signals, "PUT", "Bearish Pin Bar",
                "ذيل علوي طويل يشير إلى رفض الصعود"
            )

    # 12. Five-minute trend
    ema20_5 = ema(closes5, 20)
    ema50_5 = ema(closes5, 50)

    if ema20_5 is not None and ema50_5 is not None:
        if closes5[-1] > ema20_5 > ema50_5:
            add_signal(
                signals, "CALL", "5m Trend",
                "الاتجاه العام على 5 دقائق صاعد"
            )
        elif closes5[-1] < ema20_5 < ema50_5:
            add_signal(
                signals, "PUT", "5m Trend",
                "الاتجاه العام على 5 دقائق هابط"
            )

    # 13. MACD
    fast_now = ema(closes, 12)
    slow_now = ema(closes, 26)
    fast_prev = ema(closes[:-1], 12)
    slow_prev = ema(closes[:-1], 26)

    if all(v is not None for v in (
        fast_now, slow_now, fast_prev, slow_prev
    )):
        if fast_prev <= slow_prev and fast_now > slow_now:
            add_signal(
                signals, "CALL", "MACD Direction",
                "تقاطع المتوسط السريع فوق البطيء"
            )
        elif fast_prev >= slow_prev and fast_now < slow_now:
            add_signal(
                signals, "PUT", "MACD Direction",
                "تقاطع المتوسط السريع تحت البطيء"
            )

    if not signals:
        return None

    call_signals = [s for s in signals if s["direction"] == "CALL"]
    put_signals = [s for s in signals if s["direction"] == "PUT"]

    # لا نرسل اتجاهين متعارضين على نفس السوق
    if call_signals and put_signals:
        logger.info(
            "Conflicting strategies for %s: CALL=%s PUT=%s",
            symbol,
            [s["name"] for s in call_signals],
            [s["name"] for s in put_signals],
        )
        return None

    selected = call_signals if call_signals else put_signals
    direction = selected[0]["direction"]

    matching_strategies = list(dict.fromkeys(
        s["name"] for s in selected
    ))

    strategy_reasons = list(dict.fromkeys(
        f'{s["name"]}: {s["reason"]}' for s in selected
    ))

    trend = "UP" if price > (ema50 or price) else "DOWN"

    result = {
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "pattern": matching_strategies[0],
        "score": len(selected),
        "rsi": current_rsi,
        "adx": 0,
        "atr": average_range,
        "body_ratio": body / candle_range if candle_range else 0,
        "expiry_minutes": 1,
        "status": "DEMO_ONLY",
        "strategy_votes": len(selected),
        "confirmed_groups": len(selected),
        "matching_strategies": matching_strategies,
        "opposing_strategies": [],
        "strategy_reasons": strategy_reasons,
        "support": support,
        "resistance": resistance,
        "trend": trend,
    }

    return result


def is_new_signal(symbol, signal):
    if not signal:
        return False

    strategies = tuple(signal.get("matching_strategies", []))
    key = (
        symbol,
        signal["direction"],
        strategies,
        round(float(signal["price"]), 6),
    )

    if key in _sent:
        return False

    _sent.add(key)

    if len(_sent) > 3000:
        _sent.clear()
        _sent.add(key)

    return True
