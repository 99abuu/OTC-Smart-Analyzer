
import logging
from math import isfinite

logger = logging.getLogger(__name__)

MIN_CANDLES = 60
SIGNAL_COOLDOWN_SECONDS = 3000


def valid_candles(candles):
    if not isinstance(candles, list):
        return []

    result = []

    for candle in candles:
        if not isinstance(candle, dict):
            continue

        try:
            o = float(candle["open"])
            h = float(candle["high"])
            l = float(candle["low"])
            c = float(candle["close"])

            if not all(isfinite(x) for x in (o, h, l, c)):
                continue

            if h < max(o, c, l) or l > min(o, c, h):
                continue

            result.append({
                **candle,
                "open": o,
                "high": h,
                "low": l,
                "close": c
            })

        except (KeyError, TypeError, ValueError):
            continue

    return result


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period

    for value in values[period:]:
        current = (value * multiplier) + (
            current * (1 - multiplier)
        )

    return current


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
        return 100 if gains > 0 else 50

    rs = gains / losses
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(len(candles) - period, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close)
        )

        ranges.append(true_range)

    return sum(ranges) / len(ranges)


def candle_metrics(candle):
    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]

    full_range = h - l

    if full_range <= 0:
        return None

    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    return {
        "body": body,
        "range": full_range,
        "body_ratio": body / full_range,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "close_position": (c - l) / full_range
    }


def get_trend(candles):
    closes = [c["close"] for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)

    if ema20 is None or ema50 is None:
        return "UNKNOWN", ema20, ema50, ema200

    if ema200 is not None:
        if ema50 > ema200 and ema20 > ema50:
            return "UP", ema20, ema50, ema200

        if ema50 < ema200 and ema20 < ema50:
            return "DOWN", ema20, ema50, ema200

    if ema20 > ema50:
        return "UP", ema20, ema50, ema200

    if ema20 < ema50:
        return "DOWN", ema20, ema50, ema200

    return "SIDEWAYS", ema20, ema50, ema200


def find_support_resistance(candles, lookback=30):
    history = candles[-(lookback + 1):-1]

    if len(history) < 15:
        return None, None

    support = min(c["low"] for c in history)
    resistance = max(c["high"] for c in history)

    return support, resistance


def strategy_support_rejection(candles, support, resistance, average_atr):
    if support is None or average_atr is None:
        return None

    candle = candles[-1]
    metrics = candle_metrics(candle)

    if not metrics:
        return None

    distance = abs(candle["low"] - support)
    near_level = distance <= max(
        average_atr * 0.25,
        candle["close"] * 0.0005
    )

    bullish_rejection = (
        candle["close"] > candle["open"]
        and metrics["lower_wick"] >= metrics["body"] * 1.2
        and metrics["close_position"] >= 0.65
    )

    if near_level and bullish_rejection:
        return (
            "CALL",
            "Support Rejection: ارتداد مؤكد نسبياً من الدعم"
        )

    return None


def strategy_resistance_rejection(candles, support, resistance, average_atr):
    if resistance is None or average_atr is None:
        return None

    candle = candles[-1]
    metrics = candle_metrics(candle)

    if not metrics:
        return None

    distance = abs(candle["high"] - resistance)
    near_level = distance <= max(
        average_atr * 0.25,
        candle["close"] * 0.0005
    )

    bearish_rejection = (
        candle["close"] < candle["open"]
        and metrics["upper_wick"] >= metrics["body"] * 1.2
        and metrics["close_position"] <= 0.35
    )

    if near_level and bearish_rejection:
        return (
            "PUT",
            "Resistance Rejection: رفض سعري عند المقاومة"
        )

    return None


def strategy_trend_pullback(candles_1m, candles_5m, trend_1m, trend_5m):
    if trend_1m not in ("UP", "DOWN"):
        return None

    if trend_5m != trend_1m:
        return None

    closes = [c["close"] for c in candles_1m]
    current = candles_1m[-1]
    previous = candles_1m[-2]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    current_rsi = rsi(closes)

    if ema20 is None or ema50 is None or current_rsi is None:
        return None

    metrics = candle_metrics(current)

    if not metrics or metrics["body_ratio"] < 0.35:
        return None

    if trend_1m == "UP":
        pullback = (
            previous["low"] <= ema20 * 1.0015
            or previous["low"] <= ema50 * 1.0015
        )

        recovery = (
            current["close"] > current["open"]
            and current["close"] > ema20
            and current["close"] > previous["high"]
            and 45 <= current_rsi <= 68
        )

        if pullback and recovery:
            return (
                "CALL",
                "Trend Pullback: تصحيح ثم استئناف الاتجاه الصاعد"
            )

    if trend_1m == "DOWN":
        pullback = (
            previous["high"] >= ema20 * 0.9985
            or previous["high"] >= ema50 * 0.9985
        )

        recovery = (
            current["close"] < current["open"]
            and current["close"] < ema20
            and current["close"] < previous["low"]
            and 32 <= current_rsi <= 55
        )

        if pullback and recovery:
            return (
                "PUT",
                "Trend Pullback: تصحيح ثم استئناف الاتجاه الهابط"
            )

    return None


def strategy_breakout_retest(candles, average_atr):
    if len(candles) < 25 or average_atr is None:
        return None

    previous = candles[-2]
    current = candles[-1]

    reference = candles[-22:-2]

    if len(reference) < 15:
        return None

    resistance = max(c["high"] for c in reference)
    support = min(c["low"] for c in reference)

    tolerance = average_atr * 0.25

    bullish_retest = (
        previous["close"] > resistance
        and current["low"] <= resistance + tolerance
        and current["close"] > resistance
        and current["close"] > current["open"]
    )

    bearish_retest = (
        previous["close"] < support
        and current["high"] >= support - tolerance
        and current["close"] < support
        and current["close"] < current["open"]
    )

    if bullish_retest:
        return (
            "CALL",
            "Breakout Retest: اختراق المقاومة وإعادة اختبارها"
        )

    if bearish_retest:
        return (
            "PUT",
            "Breakout Retest: كسر الدعم وإعادة اختباره"
        )

    return None


def analyze_signal(candles_1m, candles_5m, symbol=None):
    candles_1m = valid_candles(candles_1m)
    candles_5m = valid_candles(candles_5m)

    if len(candles_1m) < MIN_CANDLES:
        return None

    if len(candles_5m) < 50:
        return None

    closes_1m = [c["close"] for c in candles_1m]

    current = candles_1m[-1]
    price = current["close"]

    average_atr = atr(candles_1m)

    if average_atr is None or average_atr <= 0:
        return None

    # تجاهل الحركة الضعيفة جداً أو الشمعة غير الطبيعية
    current_metrics = candle_metrics(current)

    if not current_metrics:
        return None

    if current_metrics["range"] < average_atr * 0.35:
        return None

    if current_metrics["range"] > average_atr * 3.0:
        return None

    trend_1m, ema20_1m, ema50_1m, ema200_1m = get_trend(candles_1m)
    trend_5m, ema20_5m, ema50_5m, ema200_5m = get_trend(candles_5m)

    current_rsi = rsi(closes_1m)

    support, resistance = find_support_resistance(candles_1m)

    strategies = [
        strategy_support_rejection(
            candles_1m, support, resistance, average_atr
        ),
        strategy_resistance_rejection(
            candles_1m, support, resistance, average_atr
        ),
        strategy_trend_pullback(
            candles_1m, candles_5m, trend_1m, trend_5m
        ),
        strategy_breakout_retest(
            candles_1m, average_atr
        )
    ]

    calls = []
    puts = []

    for result in strategies:
        if result is None:
            continue

        direction, reason = result

        if direction == "CALL":
            calls.append((reason, direction))
        elif direction == "PUT":
            puts.append((reason, direction))

    # لا نرسل إشارات متضاربة
    if calls and puts:
        logger.info(
            "Signal conflict | symbol=%s | CALL=%s | PUT=%s",
            symbol,
            len(calls),
            len(puts)
        )
        return None

    selected = calls if calls else puts

    if not selected:
        return None

    direction = selected[0][1]

    matching_strategies = [item[0].split(":")[0] for item in selected]
    strategy_reasons = [item[0] for item in selected]

    # تقييم وصفي للنقاط، وليس احتمال نجاح
    score = min(100, 70 + (len(selected) * 7))

    return {
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "pattern": ", ".join(matching_strategies),
        "score": score,
        "rsi": current_rsi,
        "adx": 0,
        "atr": average_atr,
        "body_ratio": current_metrics["body_ratio"],
        "expiry_minutes": 1,
        "status": "DEMO_ONLY",
        "strategy_votes": len(selected),
        "confirmed_groups": matching_strategies,
        "matching_strategies": matching_strategies,
        "opposing_strategies": [],
        "strategy_reasons": strategy_reasons,
        "support": support,
        "resistance": resistance,
        "trend": trend_1m,
        "trend_5m": trend_5m,
        "ema200": ema200_1m
    }


_sent = set()


def is_new_signal(signal):
    if not signal:
        return False

    key = (
        signal.get("symbol"),
        signal.get("direction"),
        tuple(signal.get("matching_strategies", [])),
        round(float(signal.get("price", 0)), 6)
    )

    if key in _sent:
        return False

    _sent.add(key)

    if len(_sent) > 3000:
        _sent.clear()
        _sent.add(key)

    return True
