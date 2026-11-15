
import logging
import time
from math import isfinite

logger = logging.getLogger(__name__)

MIN_CANDLES = 60
EMA_LONG_PERIOD = 200
ADX_PERIOD = 14
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

            if min(o, h, l, c) <= 0:
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


def true_ranges(candles):
    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close)
        ))

    return ranges


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = true_ranges(candles)

    return sum(ranges[-period:]) / period


def adx(candles, period=ADX_PERIOD):
    """
    Wilder ADX calculation.
    Returns ADX, +DI and -DI.
    """

    if len(candles) < (period * 2) + 1:
        return None, None, None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        plus = (
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus = (
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    smooth_tr = sum(trs[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])

    dx_values = []

    def calculate_di():
        if smooth_tr <= 0:
            return 0.0, 0.0

        plus_di = 100 * smooth_plus / smooth_tr
        minus_di = 100 * smooth_minus / smooth_tr

        return plus_di, minus_di

    for i in range(period, len(trs)):
        smooth_tr = (
            smooth_tr - smooth_tr / period + trs[i]
        )

        smooth_plus = (
            smooth_plus - smooth_plus / period + plus_dm[i]
        )

        smooth_minus = (
            smooth_minus - smooth_minus / period + minus_dm[i]
        )

        plus_di, minus_di = calculate_di()

        denominator = plus_di + minus_di

        dx = (
            100 * abs(plus_di - minus_di) / denominator
            if denominator > 0
            else 0.0
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None, None, None

    current_adx = sum(dx_values[:period]) / period

    for value in dx_values[period:]:
        current_adx = (
            current_adx * (period - 1) + value
        ) / period

    plus_di, minus_di = calculate_di()

    return current_adx, plus_di, minus_di


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
    ema200 = ema(closes, EMA_LONG_PERIOD)

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
    metrics,
    current_adx,
    plus_di,
    minus_di,
    average_atr
):
    """
    Quality score, not a win probability.
    """

    score = 45.0

    score += min(12, max(0, len(selected) - 1) * 6)
    score += metrics["body_ratio"] * 10

    if direction == "CALL":
        score += metrics["close_position"] * 8
    else:
        score += (1 - metrics["close_position"]) * 8

    if (
        (direction == "CALL" and trend == "UP")
        or (direction == "PUT" and trend == "DOWN")
    ):
        score += 7

    elif trend in ("UP", "DOWN"):
        score -= 3

    if ema200 is not None:
        if (
            direction == "CALL" and price > ema200
        ) or (
            direction == "PUT" and price < ema200
        ):
            score += 5
        else:
            score -= 3

    if current_rsi is not None:
        if direction == "CALL":
            if 45 <= current_rsi <= 68:
                score += 4
            elif current_rsi > 75:
                score -= 4
            elif current_rsi < 30:
                score -= 2

        else:
            if 32 <= current_rsi <= 55:
                score += 4
            elif current_rsi < 25:
                score -= 4
            elif current_rsi > 70:
                score -= 2

    if current_adx is not None:
        if current_adx >= 25:
            score += 5
        elif current_adx >= 20:
            score += 3
        elif current_adx < 12:
            score -= 3

    if plus_di is not None and minus_di is not None:
        if direction == "CALL" and plus_di > minus_di:
            score += 3
        elif direction == "PUT" and minus_di > plus_di:
            score += 3
        else:
            score -= 2

    if average_atr and average_atr > 0:
        range_ratio = metrics["range"] / average_atr

        if 0.65 <= range_ratio <= 1.8:
            score += 3
        elif range_ratio > 2.3:
            score -= 5

    return round(max(0, min(95, score)), 1)


def select_trade_duration(signal):
    """
    Experimental rule-based duration selection.
    Returns 60 or 120 seconds and an explanation.
    This is not an AI model or a win probability.
    """

    direction = signal.get("direction")
    trend = signal.get("trend")
    adx_value = signal.get("adx")
    body_ratio = signal.get("body_ratio")
    range_ratio = signal.get("range_ratio")
    rsi_value = signal.get("rsi")
    strategies = signal.get("matching_strategies", [])

    trend_aligned = (
        (direction == "CALL" and trend == "UP")
        or (direction == "PUT" and trend == "DOWN")
    )

    continuation_signal = (
        "Trend Pullback" in strategies
        or "Breakout Retest" in strategies
    )

    if (
        trend_aligned
        and continuation_signal
        and adx_value is not None
        and adx_value >= 22
        and body_ratio is not None
        and body_ratio >= 0.40
        and range_ratio is not None
        and 0.65 <= range_ratio <= 1.8
        and rsi_value is not None
        and 30 <= rsi_value <= 70
    ):
        return (
            120,
            "اتجاه متوافق وزخم مناسب واستمرار محتمل للحركة"
        )

    if (
        "Support Rejection" in strategies
        or "Resistance Rejection" in strategies
    ):
        return (
            60,
            "ارتداد من دعم أو مقاومة؛ اختيار مدة أقصر للاختبار"
        )

    if not trend_aligned:
        return (
            60,
            "الاتجاه غير متوافق بوضوح؛ اختيار مدة أقصر للاختبار"
        )

    return (
        60,
        "شروط الاستمرار لمدة دقيقتين غير مكتملة"
    )


def analyze_signal(candles_1m, candles_5m=None, symbol=None):
    # التحليل بالكامل على فريم الدقيقة.
    # لا نستخدم بيانات 5m.

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

    if current_metrics["range"] < average_atr * 0.35:
        return None

    if current_metrics["range"] > average_atr * 3.0:
        return None

    trend, ema20, ema50, ema200 = get_trend(candles)

    current_rsi = rsi(closes)

    current_adx, plus_di, minus_di = adx(candles)

    range_ratio = (
        current_metrics["range"] / average_atr
        if average_atr > 0
        else None
    )

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
        metrics=current_metrics,
        current_adx=current_adx,
        plus_di=plus_di,
        minus_di=minus_di,
        average_atr=average_atr
    )

    signal = {
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "pattern": ", ".join(matching_strategies),
        "score": score,
        "rsi": current_rsi,
        "adx": current_adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "atr": average_atr,
        "body_ratio": current_metrics["body_ratio"],
        "range_ratio": range_ratio,
        "close_position": current_metrics["close_position"],
        "expiry_minutes": 1,
        "duration_seconds": 60,
        "selected_duration": 60,
        "duration_reason": "",
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
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "analysis_timeframe": "1m"
    }

    selected_duration, duration_reason = (
        select_trade_duration(signal)
    )

    signal["selected_duration"] = selected_duration
    signal["duration_seconds"] = selected_duration
    signal["expiry_minutes"] = selected_duration // 60
    signal["duration_reason"] = duration_reason

    return signal


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

    expired = [
        item for item, timestamp in _sent.items()
        if now - timestamp >= SIGNAL_COOLDOWN_SECONDS
    ]

    for item in expired:
        _sent.pop(item, None)

    return True
