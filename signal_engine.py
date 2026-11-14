
import logging
import time
from math import isfinite

logger = logging.getLogger(__name__)

MIN_CANDLES = 60
SIGNAL_COOLDOWN_SECONDS = 3000

_sent = {}


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
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )

    return current


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100 if avg_gain > 0 else 50

    rs = avg_gain / avg_loss

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

    price = closes[-1]

    if ema200 is not None:

        if (
            price > ema200
            and ema50 > ema200
            and ema20 > ema50
        ):
            return "UP", ema20, ema50, ema200

        if (
            price < ema200
            and ema50 < ema200
            and ema20 < ema50
        ):
            return "DOWN", ema20, ema50, ema200

        return "SIDEWAYS", ema20, ema50, ema200

    if ema20 > ema50:
        return "UP", ema20, ema50, ema200

    if ema20 < ema50:
        return "DOWN", ema20, ema50, ema200

    return "SIDEWAYS", ema20, ema50, ema200


def find_support_resistance(candles, lookback=40):
    history = candles[-(lookback + 1):-1]

    if len(history) < 15:
        return None, None

    price = candles[-1]["close"]

    pivot_lows = []
    pivot_highs = []

    for i in range(1, len(history) - 1):

        previous = history[i - 1]
        current = history[i]
        following = history[i + 1]

        if (
            current["low"] <= previous["low"]
            and current["low"] < following["low"]
        ):
            pivot_lows.append(current["low"])

        if (
            current["high"] >= previous["high"]
            and current["high"] > following["high"]
        ):
            pivot_highs.append(current["high"])

    supports = [
        level for level in pivot_lows
        if level <= price
    ]

    resistances = [
        level for level in pivot_highs
        if level >= price
    ]

    support = (
        max(supports)
        if supports
        else min(c["low"] for c in history)
    )

    resistance = (
        min(resistances)
        if resistances
        else max(c["high"] for c in history)
    )

    return support, resistance


def strategy_support_rejection(
    candles,
    support,
    resistance,
    average_atr
):
    if support is None or average_atr is None:
        return None

    candle = candles[-1]
    metrics = candle_metrics(candle)

    if not metrics:
        return None

    tolerance = average_atr * 0.30

    touched_level = (
        candle["low"] <= support + tolerance
        and candle["low"] >= support - average_atr * 0.45
    )

    bullish_rejection = (
        candle["close"] > candle["open"]
        and candle["close"] > support
        and metrics["lower_wick"] >= metrics["body"] * 1.2
        and metrics["lower_wick"] >= metrics["range"] * 0.25
        and metrics["close_position"] >= 0.68
        and metrics["body_ratio"] >= 0.15
    )

    if touched_level and bullish_rejection:
        return (
            "CALL",
            "Support Rejection: ارتداد مؤكد من منطقة الدعم"
        )

    return None


def strategy_resistance_rejection(
    candles,
    support,
    resistance,
    average_atr
):
    if resistance is None or average_atr is None:
        return None

    candle = candles[-1]
    metrics = candle_metrics(candle)

    if not metrics:
        return None

    tolerance = average_atr * 0.30

    touched_level = (
        candle["high"] >= resistance - tolerance
        and candle["high"] <= resistance + average_atr * 0.45
    )

    bearish_rejection = (
        candle["close"] < candle["open"]
        and candle["close"] < resistance
        and metrics["upper_wick"] >= metrics["body"] * 1.2
        and metrics["upper_wick"] >= metrics["range"] * 0.25
        and metrics["close_position"] <= 0.32
        and metrics["body_ratio"] >= 0.15
    )

    if touched_level and bearish_rejection:
        return (
            "PUT",
            "Resistance Rejection: رفض مؤكد من منطقة المقاومة"
        )

    return None


def strategy_trend_pullback(candles, trend, average_atr=None):
    if trend not in ("UP", "DOWN"):
        return None

    closes = [c["close"] for c in candles]

    current = candles[-1]
    previous = candles[-2]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    current_rsi = rsi(closes)

    if (
        ema20 is None
        or ema50 is None
        or current_rsi is None
        or average_atr is None
    ):
        return None

    metrics = candle_metrics(current)

    if not metrics or metrics["body_ratio"] < 0.35:
        return None

    if trend == "UP":

        pullback = (
            previous["low"] <= ema20 + average_atr * 0.25
            and previous["low"] >= ema50 - average_atr * 0.80
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
                "Trend Pullback: تصحيح مؤكد واستئناف الاتجاه الصاعد"
            )

    if trend == "DOWN":

        pullback = (
            previous["high"] >= ema20 - average_atr * 0.25
            and previous["high"] <= ema50 + average_atr * 0.80
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
                "Trend Pullback: تصحيح مؤكد واستئناف الاتجاه الهابط"
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

    tolerance = average_atr * 0.30
    breakout_buffer = average_atr * 0.05

    bullish_retest = (
        previous["close"] > resistance + breakout_buffer
        and previous["close"] > previous["open"]
        and current["low"] <= resistance + tolerance
        and current["low"] >= resistance - average_atr * 0.45
        and current["close"] > resistance
        and current["close"] > current["open"]
    )

    bearish_retest = (
        previous["close"] < support - breakout_buffer
        and previous["close"] < previous["open"]
        and current["high"] >= support - tolerance
        and current["high"] <= support + average_atr * 0.45
        and current["close"] < support
        and current["close"] < current["open"]
    )

    if bullish_retest:
        return (
            "CALL",
            "Breakout Retest: اختراق مؤكد وإعادة اختبار المقاومة"
        )

    if bearish_retest:
        return (
            "PUT",
            "Breakout Retest: كسر مؤكد وإعادة اختبار الدعم"
        )

    return None


def calculate_signal_score(
    selected,
    direction,
    trend,
    ema200,
    price,
    current_rsi,
    metrics
):
    score = 45

    # عدد الاستراتيجيات المتوافقة
    score += min(16, (len(selected) - 1) * 8)

    # قوة جسم شمعة الإشارة
    score += min(12, metrics["body_ratio"] * 12)

    # موقع الإغلاق داخل الشمعة
    if direction == "CALL":
        score += metrics["close_position"] * 10

    else:
        score += (1 - metrics["close_position"]) * 10

    # توافق الاتجاه العام
    if (
        (direction == "CALL" and trend == "UP")
        or (direction == "PUT" and trend == "DOWN")
    ):
        score += 8

    # توافق السعر مع EMA200
    if ema200 is not None:

        if (
            direction == "CALL"
            and price > ema200
        ):
            score += 5

        elif (
            direction == "PUT"
            and price < ema200
        ):
            score += 5

    # RSI غير متطرف
    if current_rsi is not None:

        if direction == "CALL" and 45 <= current_rsi <= 68:
            score += 4

        elif direction == "PUT" and 32 <= current_rsi <= 55:
            score += 4

    return round(min(95, score), 1)


def analyze_signal(candles_1m, candles_5m=None, symbol=None):

    # جميع الاستراتيجيات تعمل على فريم الدقيقة فقط.
    # candles_5m محفوظ للتوافق مع النسخ السابقة.
    candles = valid_candles(candles_1m)

    if len(candles) < MIN_CANDLES:
        return None

    closes = [c["close"] for c in candles]

    current = candles[-1]
    price = current["close"]

    average_atr = atr(candles)

    if average_atr is None or average_atr <= 0:
        return None

    current_metrics = candle_metrics(current)

    if not current_metrics:
        return None

    # تجنب الشموع شديدة الهدوء أو شديدة العنف
    if current_metrics["range"] < average_atr * 0.35:
        return None

    if current_metrics["range"] > average_atr * 3.0:
        return None

    trend, ema20, ema50, ema200 = get_trend(candles)

    current_rsi = rsi(closes)

    support, resistance = find_support_resistance(candles)

    strategies = [
        strategy_support_rejection(
            candles,
            support,
            resistance,
            average_atr
        ),

        strategy_resistance_rejection(
            candles,
            support,
            resistance,
            average_atr
        ),

        strategy_trend_pullback(
            candles,
            trend,
            average_atr
        ),

        strategy_breakout_retest(
            candles,
            average_atr
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

    # منع الإشارات المتضاربة
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

    matching_strategies = [
        item[0].split(":")[0]
        for item in selected
    ]

    strategy_reasons = [
        item[0]
        for item in selected
    ]

    score = calculate_signal_score(
        selected=selected,
        direction=direction,
        trend=trend,
        ema200=ema200,
        price=price,
        current_rsi=current_rsi,
        metrics=current_metrics
    )

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
        "trend": trend,
        "trend_5m": "NOT_USED",
        "ema200": ema200,
        "analysis_timeframe": "1m"
    }


def is_new_signal(signal):

    if not signal:
        return False

    now = time.monotonic()

    key = (
        signal.get("symbol"),
        signal.get("direction"),
        tuple(signal.get("matching_strategies", []))
    )

    previous_time = _sent.get(key)

    if previous_time is not None:
        if now - previous_time < SIGNAL_COOLDOWN_SECONDS:
            return False

    _sent[key] = now

    # تنظيف السجلات القديمة
    expired = [
        item for item, timestamp in _sent.items()
        if now - timestamp >= SIGNAL_COOLDOWN_SECONDS
    ]

    for item in expired:
        _sent.pop(item, None)

    return True
