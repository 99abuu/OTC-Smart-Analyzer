
import logging

logger = logging.getLogger(__name__)


# =====================================
# إعدادات الاستراتيجية
# =====================================

MIN_SCORE = 8
RSI_PERIOD = 14
TREND_PERIOD_FAST = 9
TREND_PERIOD_SLOW = 21
SR_LOOKBACK = 20


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
# RSI
# =====================================

def calculate_rsi(values, period=RSI_PERIOD):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

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
# اتجاه الشمعة
# =====================================

def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "CALL"

    if candle["close"] < candle["open"]:
        return "PUT"

    return None


# =====================================
# أنماط الابتلاع
# =====================================

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


# =====================================
# تحليل الذيول والرفض
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

    body_ratio = body / total_range
    upper_ratio = upper_wick / total_range
    lower_ratio = lower_wick / total_range

    close_position = (
        candle["close"] - candle["low"]
    ) / total_range

    return {
        "body_ratio": body_ratio,
        "upper_ratio": upper_ratio,
        "lower_ratio": lower_ratio,
        "close_position": close_position,
        "range": total_range
    }


def bullish_rejection(candle):
    data = candle_behavior(candle)

    if data is None:
        return False

    return (
        data["lower_ratio"] >= 0.40
        and data["close_position"] >= 0.65
        and data["upper_ratio"] <= 0.25
    )


def bearish_rejection(candle):
    data = candle_behavior(candle)

    if data is None:
        return False

    return (
        data["upper_ratio"] >= 0.40
        and data["close_position"] <= 0.35
        and data["lower_ratio"] <= 0.25
    )


# =====================================
# متوسط نطاق الشموع
# =====================================

def average_range(candles):
    if not candles:
        return None

    ranges = [
        c["high"] - c["low"]
        for c in candles
    ]

    return sum(ranges) / len(ranges)


# =====================================
# تحليل الدعم والمقاومة
# =====================================

def support_resistance_filter(
    candles,
    direction,
    current_price,
    atr
):
    if len(candles) < SR_LOOKBACK + 1 or atr <= 0:
        return False

    history = candles[-(SR_LOOKBACK + 1):-1]

    resistance = max(c["high"] for c in history)
    support = min(c["low"] for c in history)

    distance = atr * 0.20

    if direction == "CALL":
        # تجنب الشراء مباشرة أسفل مقاومة قريبة
        if (
            current_price < resistance
            and resistance - current_price <= distance
        ):
            return False

    if direction == "PUT":
        # تجنب البيع مباشرة فوق دعم قريب
        if (
            current_price > support
            and current_price - support <= distance
        ):
            return False

    return True


# =====================================
# المحرك الرئيسي
# =====================================

def analyze_signal(candles_1m, candles_5m):

    if len(candles_1m) < 30 or len(candles_5m) < 25:
        return None

    required = ("open", "high", "low", "close")

    for candle in candles_1m[-30:] + candles_5m[-25:]:
        if not all(
            isinstance(candle.get(field), (int, float))
            for field in required
        ):
            return None

        if candle["high"] < candle["low"]:
            return None

    closes_1m = [c["close"] for c in candles_1m]
    closes_5m = [c["close"] for c in candles_5m]

    # ---------------------------------
    # 1. اتجاه السوق على 5 دقائق
    # ---------------------------------

    fast = ema(closes_5m, TREND_PERIOD_FAST)
    slow = ema(closes_5m, TREND_PERIOD_SLOW)

    previous_fast = ema(
        closes_5m[:-3],
        TREND_PERIOD_FAST
    )

    if fast is None or slow is None or previous_fast is None:
        return None

    if fast > slow:
        direction = "CALL"
    elif fast < slow:
        direction = "PUT"
    else:
        return None

    # يجب أن يتحرك المتوسط السريع في اتجاه الصفقة
    if direction == "CALL" and fast <= previous_fast:
        return None

    if direction == "PUT" and fast >= previous_fast:
        return None

    # ---------------------------------
    # 2. تحليل الشموع
    # ---------------------------------

    previous = candles_1m[-2]
    current = candles_1m[-1]

    if candle_direction(current) != direction:
        return None

    behavior = candle_behavior(current)

    if behavior is None:
        return None

    if direction == "CALL":
        engulfing = bullish_engulfing(previous, current)
        rejection = bullish_rejection(current)
    else:
        engulfing = bearish_engulfing(previous, current)
        rejection = bearish_rejection(current)

    # لا دخول دون نمط شمعة واضح
    if not engulfing and not rejection:
        return None

    pattern = (
        "Engulfing"
        if engulfing
        else "Wick Rejection"
    )

    # ---------------------------------
    # 3. قياس التذبذب
    # ---------------------------------

    atr = average_range(candles_1m[-15:])

    if atr is None or atr <= 0:
        return None

    current_range = behavior["range"]

    # تجنب الشموع شديدة الاندفاع
    if current_range > atr * 2.2:
        return None

    if current_range < atr * 0.25:
        return None

    # ---------------------------------
    # 4. مؤشر RSI
    # ---------------------------------

    rsi = calculate_rsi(closes_1m)

    if rsi is None:
        return None

    if direction == "CALL":
        if not 52 <= rsi <= 68:
            return None

    if direction == "PUT":
        if not 32 <= rsi <= 48:
            return None

    # ---------------------------------
    # 5. الزخم
    # ---------------------------------

    if len(closes_1m) < 4:
        return None

    momentum = (
        closes_1m[-1] - closes_1m[-4]
    )

    if direction == "CALL" and momentum <= 0:
        return None

    if direction == "PUT" and momentum >= 0:
        return None

    # ---------------------------------
    # 6. الدعم والمقاومة
    # ---------------------------------

    sr_valid = support_resistance_filter(
        candles_1m,
        direction,
        current["close"],
        atr
    )

    if not sr_valid:
        return None

    # ---------------------------------
    # 7. تقييم جودة الإشارة
    # ---------------------------------

    score = 0

    # اتجاه EMA
    score += 2

    # ميل EMA
    score += 1

    # نمط الشمعة
    score += 2

    # جودة جسم الشمعة
    if behavior["body_ratio"] >= 0.35:
        score += 1

    # قوة الإغلاق
    if direction == "CALL":
        if behavior["close_position"] >= 0.75:
            score += 1

    if direction == "PUT":
        if behavior["close_position"] <= 0.25:
            score += 1

    # الزخم
    score += 1

    # RSI مناسب
    score += 1

    # عدم وجود مقاومة أو دعم قريب يعاكس الدخول
    score += 1

    if score < MIN_SCORE:
        return None

    logger.info(
        "SIGNAL: %s | score=%s | RSI=%.2f | pattern=%s",
        direction,
        score,
        rsi,
        pattern
    )

    return {
        "direction": direction,
        "entry_reference_price": current["close"],
        "pattern": pattern,
        "score": score,
        "rsi": round(rsi, 2),
        "candle_body_ratio": round(
            behavior["body_ratio"], 2
        ),
        "expiry_minutes": 1,
        "status": "DEMO_ONLY"
    }


# =====================================
# منع تكرار الإشارات
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

    if len(sent_signals) > 1000:
        sent_signals.clear()
        sent_signals.add(key)

    return True
