
import logging

logger = logging.getLogger(__name__)

# =====================================
# STRATEGY SETTINGS
# =====================================

MIN_SCORE = 9

EMA_TREND_FAST = 50
EMA_TREND_SLOW = 200

EMA_ENTRY_FAST = 9
EMA_ENTRY_SLOW = 21

RSI_PERIOD = 14
ADX_PERIOD = 14
ATR_PERIOD = 14

MIN_ADX = 20
MIN_ATR_BODY_RATIO = 0.35
MAX_ATR_BODY_RATIO = 2.0


# =====================================
# EMA
# =====================================

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


# =====================================
# RSI - WILDER
# =====================================

def calculate_rsi(values, period=RSI_PERIOD):

    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

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


# =====================================
# ATR - WILDER
# =====================================

def calculate_atr(candles, period=ATR_PERIOD):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    atr_value = sum(true_ranges[:period]) / period

    for value in true_ranges[period:]:

        atr_value = (
            atr_value * (period - 1) + value
        ) / period

    return atr_value


# =====================================
# ADX - WILDER
# =====================================

def calculate_adx(candles, period=ADX_PERIOD):

    if len(candles) < (period * 2) + 2:
        return None

    trs = []
    plus_dms = []
    minus_dms = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        plus_dm = (
            up_move
            if up_move > down_move and up_move > 0
            else 0
        )

        minus_dm = (
            down_move
            if down_move > up_move and down_move > 0
            else 0
        )

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    if len(trs) < period * 2:
        return None

    smoothed_tr = sum(trs[:period])
    smoothed_plus = sum(plus_dms[:period])
    smoothed_minus = sum(minus_dms[:period])

    dx_values = []

    for i in range(period, len(trs)):

        if i > period:

            smoothed_tr = (
                smoothed_tr
                - smoothed_tr / period
                + trs[i]
            )

            smoothed_plus = (
                smoothed_plus
                - smoothed_plus / period
                + plus_dms[i]
            )

            smoothed_minus = (
                smoothed_minus
                - smoothed_minus / period
                + minus_dms[i]
            )

        if smoothed_tr <= 0:
            continue

        plus_di = 100 * smoothed_plus / smoothed_tr
        minus_di = 100 * smoothed_minus / smoothed_tr

        denominator = plus_di + minus_di

        if denominator <= 0:
            dx_values.append(0)
        else:
            dx_values.append(
                100 * abs(plus_di - minus_di) / denominator
            )

    if len(dx_values) < period:
        return None

    adx_value = sum(dx_values[:period]) / period

    for value in dx_values[period:]:

        adx_value = (
            adx_value * (period - 1) + value
        ) / period

    return adx_value


# =====================================
# CANDLE VALIDATION
# =====================================

def valid_candles(candles):

    required = ("open", "high", "low", "close")

    for candle in candles:

        if not all(
            isinstance(candle.get(field), (int, float))
            for field in required
        ):
            return False

        if candle["high"] < candle["low"]:
            return False

        if not (
            candle["low"] <= candle["open"] <= candle["high"]
            and candle["low"] <= candle["close"] <= candle["high"]
        ):
            return False

    return True


# =====================================
# CANDLE ANALYSIS
# =====================================

def candle_behavior(candle):

    body = abs(candle["close"] - candle["open"])
    total_range = candle["high"] - candle["low"]

    if total_range <= 0:
        return None

    upper_wick = (
        candle["high"]
        - max(candle["open"], candle["close"])
    )

    lower_wick = (
        min(candle["open"], candle["close"])
        - candle["low"]
    )

    return {
        "body_ratio": body / total_range,
        "upper_ratio": upper_wick / total_range,
        "lower_ratio": lower_wick / total_range,
        "close_position": (
            candle["close"] - candle["low"]
        ) / total_range,
        "range": total_range
    }


# =====================================
# MAIN SIGNAL ENGINE
# =====================================

def analyze_signal(candles_1m, candles_5m):

    # EMA 200 يحتاج بيانات تاريخية كافية
    if len(candles_5m) < 250:
        return None

    if len(candles_1m) < 60:
        return None

    if not valid_candles(candles_1m[-60:]):
        return None

    if not valid_candles(candles_5m[-250:]):
        return None

    closes_1m = [
        c["close"] for c in candles_1m
    ]

    closes_5m = [
        c["close"] for c in candles_5m
    ]

    # =================================
    # 1. MAIN TREND: EMA 50 / EMA 200
    # =================================

    ema50 = ema(closes_5m, EMA_TREND_FAST)
    ema200 = ema(closes_5m, EMA_TREND_SLOW)

    if ema50 is None or ema200 is None:
        return None

    trend_price = closes_5m[-1]

    if trend_price > ema50 > ema200:
        direction = "CALL"

    elif trend_price < ema50 < ema200:
        direction = "PUT"

    else:
        return None

    score = 3

    # =================================
    # 2. ADX TREND STRENGTH
    # =================================

    adx = calculate_adx(candles_5m)

    if adx is None or adx < MIN_ADX:
        return None

    score += 2

    # =================================
    # 3. ENTRY EMA 9 / EMA 21
    # =================================

    entry_fast = ema(
        closes_1m,
        EMA_ENTRY_FAST
    )

    entry_slow = ema(
        closes_1m,
        EMA_ENTRY_SLOW
    )

    if entry_fast is None or entry_slow is None:
        return None

    current = candles_1m[-1]
    previous = candles_1m[-2]

    if direction == "CALL":

        if entry_fast <= entry_slow:
            return None

    else:

        if entry_fast >= entry_slow:
            return None

    score += 2

    # =================================
    # 4. PULLBACK AND RESUMPTION
    # =================================

    recent = candles_1m[-5:-1]

    if direction == "CALL":

        pullback_found = any(
            candle["low"] <= entry_fast
            or candle["low"] <= entry_slow
            for candle in recent
        )

        resumed = (
            current["close"] > current["open"]
            and current["close"] > entry_fast
        )

    else:

        pullback_found = any(
            candle["high"] >= entry_fast
            or candle["high"] >= entry_slow
            for candle in recent
        )

        resumed = (
            current["close"] < current["open"]
            and current["close"] < entry_fast
        )

    if not pullback_found or not resumed:
        return None

    score += 2

    # =================================
    # 5. ATR VOLATILITY FILTER
    # =================================

    atr = calculate_atr(
        candles_1m[-(ATR_PERIOD + 2):-1]
    )

    if atr is None or atr <= 0:
        return None

    behavior = candle_behavior(current)

    if behavior is None:
        return None

    if behavior["range"] > atr * MAX_ATR_BODY_RATIO:
        return None

    if behavior["range"] < atr * MIN_ATR_BODY_RATIO:
        return None

    # =================================
    # 6. CANDLE QUALITY
    # =================================

    if behavior["body_ratio"] < 0.45:
        return None

    if direction == "CALL":

        if behavior["close_position"] < 0.70:
            return None

    else:

        if behavior["close_position"] > 0.30:
            return None

    score += 1

    # =================================
    # 7. RSI MOMENTUM
    # =================================

    rsi = calculate_rsi(closes_1m)

    if rsi is None:
        return None

    if direction == "CALL":

        if not 50 <= rsi <= 68:
            return None

    else:

        if not 32 <= rsi <= 50:
            return None

    score += 1

    # =================================
    # 8. SHORT-TERM MOMENTUM
    # =================================

    momentum = closes_1m[-1] - closes_1m[-4]

    if direction == "CALL" and momentum <= 0:
        return None

    if direction == "PUT" and momentum >= 0:
        return None

    # =================================
    # 9. FINAL DECISION
    # =================================

    if score < MIN_SCORE:
        return None

    pattern = "Pullback Resumption"

    logger.info(
        "NEW SIGNAL | %s | score=%s | ADX=%.2f | RSI=%.2f",
        direction,
        score,
        adx,
        rsi
    )

    return {
        "direction": direction,
        "entry_reference_price": current["close"],
        "pattern": pattern,
        "score": score,
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "atr": round(atr, 6),
        "candle_body_ratio": round(
            behavior["body_ratio"], 2
        ),
        "expiry_minutes": 1,
        "status": "DEMO_ONLY"
    }


# =====================================
# PREVENT DUPLICATE SIGNALS
# =====================================

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

    if len(sent_signals) > 3000:
        sent_signals.clear()
        sent_signals.add(key)

    return True
