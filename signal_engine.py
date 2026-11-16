import logging
import time
from math import isfinite

logger = logging.getLogger(__name__)

MIN_CANDLES = 60
EMA_LONG_PERIOD = 200
ADX_PERIOD = 14
SIGNAL_COOLDOWN_SECONDS = 3000

# إعدادات مناطق الدعم والمقاومة
SR_LOOKBACK = 60
SR_CLUSTER_ATR = 0.35
SR_MIN_TOUCHES = 2
SR_MIN_TOUCH_GAP = 3

# إعدادات الاتجاه القوي
TREND_ADX_MIN = 20
TREND_SLOPE_LOOKBACK = 5

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

def find_pivots(candles):
    """
    اكتشاف القمم والقيعان المحلية.
    لا يستخدم الشمعة الحالية لتأكيد Pivot.
    """

    highs = []
    lows = []

    for i in range(1, len(candles) - 1):
        previous = candles[i - 1]
        current = candles[i]
        following = candles[i + 1]

        if (
            current["high"] >= previous["high"]
            and current["high"] > following["high"]
        ):
            highs.append((i, current["high"]))

        if (
            current["low"] <= previous["low"]
            and current["low"] < following["low"]
        ):
            lows.append((i, current["low"]))

    return highs, lows

def market_structure(candles, lookback=45):
    """
    يقارن آخر القمم والقيعان المؤكدة.
    الاتجاه الصاعد يحتاج قمماً أعلى وقيعاناً أعلى.
    الاتجاه الهابط يحتاج قمماً أدنى وقيعاناً أدنى.
    """

    history = candles[-lookback:]

    if len(history) < 12:
        return "UNKNOWN"

    highs, lows = find_pivots(history)

    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"

    previous_high = highs[-2][1]
    current_high = highs[-1][1]

    previous_low = lows[-2][1]
    current_low = lows[-1][1]

    higher_high = current_high > previous_high
    higher_low = current_low > previous_low

    lower_high = current_high < previous_high
    lower_low = current_low < previous_low

    if higher_high and higher_low:
        return "UP"

    if lower_high and lower_low:
        return "DOWN"

    return "MIXED"

def ema_slope(values, period=20, lookback=TREND_SLOPE_LOOKBACK):
    """
    يقيس ميل EMA بمقارنة قيمتين محسوبتين من بيانات ماضية.
    """

    if len(values) < period + lookback + 2:
        return None

    current_ema = ema(values, period)
    previous_ema = ema(values[:-lookback], period)

    if current_ema is None or previous_ema is None:
        return None

    return current_ema - previous_ema

def get_trend(candles):
    closes = [c["close"] for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, EMA_LONG_PERIOD)

    if ema20 is None or ema50 is None:
        return "UNKNOWN", ema20, ema50, ema200

    price = closes[-1]
    slope20 = ema_slope(closes, 20)
    structure = market_structure(candles)

    if slope20 is None:
        return "SIDEWAYS", ema20, ema50, ema200

    bullish_stack = (
        ema20 > ema50
        and (ema200 is None or ema50 > ema200)
    )

    bearish_stack = (
        ema20 < ema50
        and (ema200 is None or ema50 < ema200)
    )

    bullish_position = price > ema20
    bearish_position = price < ema20

    if (
        bullish_stack
        and bullish_position
        and slope20 > 0
        and structure == "UP"
    ):
        return "UP", ema20, ema50, ema200

    if (
        bearish_stack
        and bearish_position
        and slope20 < 0
        and structure == "DOWN"
    ):
        return "DOWN", ema20, ema50, ema200

    return "SIDEWAYS", ema20, ema50, ema200

def find_clustered_level(pivots, price, average_atr, direction):
    """
    يجمع القمم أو القيعان المتقاربة في منطقة واحدة.
    يشترط وجود اختبارين منفصلين على الأقل.
    """

    if not pivots or average_atr is None or average_atr <= 0:
        return None

    tolerance = average_atr * SR_CLUSTER_ATR
    candidates = []

    for index, level in pivots:
        if direction == "SUPPORT" and level > price:
            continue

        if direction == "RESISTANCE" and level < price:
            continue

        cluster = [
            (pivot_index, pivot_level)
            for pivot_index, pivot_level in pivots
            if abs(pivot_level - level) <= tolerance
        ]

        cluster.sort(key=lambda item: item[0])

        distinct = []

        for item in cluster:
            if (
                not distinct
                or item[0] - distinct[-1][0] >= SR_MIN_TOUCH_GAP
            ):
                distinct.append(item)

        if len(distinct) < SR_MIN_TOUCHES:
            continue

        center = sum(item[1] for item in distinct) / len(distinct)

        if direction == "SUPPORT" and center > price:
            continue

        if direction == "RESISTANCE" and center < price:
            continue

        distance = abs(price - center)

        candidates.append({
            "level": center,
            "touches": len(distinct),
            "distance": distance
        })

    if not candidates:
        return None

    # نختار أقرب منطقة مؤكدة للسعر.
    candidates.sort(key=lambda item: item["distance"])

    return candidates[0]

def find_support_resistance(candles, lookback=SR_LOOKBACK):
    """
    يرجع مناطق دعم ومقاومة مبنية على Pivot Clusters.
    لا يخترع مستوى من أقل/أعلى شمعة إذا لم توجد اختبارات كافية.
    """

    if len(candles) < 20:
        return None, None

    history = candles[-(lookback + 1):-1]

    if len(history) < 15:
        return None, None

    average_atr = atr(candles)

    if average_atr is None or average_atr <= 0:
        return None, None

    price = candles[-1]["close"]

    highs, lows = find_pivots(history)

    support_data = find_clustered_level(
        lows,
        price,
        average_atr,
        "SUPPORT"
    )

    resistance_data = find_clustered_level(
        highs,
        price,
        average_atr,
        "RESISTANCE"
    )

    support = (
        support_data["level"]
        if support_data else None
    )

    resistance = (
        resistance_data["level"]
        if resistance_data else None
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

    if len(candles) < 3:
        return None

    current = candles[-1]
    previous = candles[-2]

    metrics = candle_metrics(current)

    if not metrics:
        return None

    tolerance = average_atr * 0.25
    breakout_buffer = average_atr * 0.08

    # الحالة الأولى: كسر كاذب للدعم.
    false_breakout = (
        previous["low"] < support - breakout_buffer
        and previous["close"] < support
        and current["close"] > support + breakout_buffer
        and current["close"] > current["open"]
        and metrics["body_ratio"] >= 0.35
        and metrics["close_position"] >= 0.70
    )

    if false_breakout:
        return (
            "CALL",
            "Support Rejection: كسر كاذب للدعم وعودة مؤكدة فوق المنطقة"
        )

    # الحالة الثانية: لمس منطقة دعم مع رفض واضح.
    touched_level = (
        current["low"] <= support + tolerance
        and current["low"] >= support - average_atr * 0.35
    )

    bullish_rejection = (
        current["close"] > current["open"]
        and current["close"] > support + average_atr * 0.03
        and metrics["lower_wick"] >= metrics["body"] * 1.3
        and metrics["lower_wick"] >= metrics["range"] * 0.28
        and metrics["close_position"] >= 0.68
        and metrics["body_ratio"] >= 0.18
    )

    if touched_level and bullish_rejection:
        return (
            "CALL",
            "Support Rejection: اختبار منطقة دعم متعددة اللمسات ورفض سعري"
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

    if len(candles) < 3:
        return None

    current = candles[-1]
    previous = candles[-2]

    metrics = candle_metrics(current)

    if not metrics:
        return None

    tolerance = average_atr * 0.25
    breakout_buffer = average_atr * 0.08

    # الحالة الأولى: كسر كاذب للمقاومة.
    false_breakout = (
        previous["high"] > resistance + breakout_buffer
        and previous["close"] > resistance
        and current["close"] < resistance - breakout_buffer
        and current["close"] < current["open"]
        and metrics["body_ratio"] >= 0.35
        and metrics["close_position"] <= 0.30
    )

    if false_breakout:
        return (
            "PUT",
            "Resistance Rejection: كسر كاذب للمقاومة وعودة مؤكدة أسفل المنطقة"
        )

    # الحالة الثانية: لمس مقاومة مع رفض واضح.
    touched_level = (
        current["high"] >= resistance - tolerance
        and current["high"] <= resistance + average_atr * 0.35
    )

    bearish_rejection = (
        current["close"] < current["open"]
        and current["close"] < resistance - average_atr * 0.03
        and metrics["upper_wick"] >= metrics["body"] * 1.3
        and metrics["upper_wick"] >= metrics["range"] * 0.28
        and metrics["close_position"] <= 0.32
        and metrics["body_ratio"] >= 0.18
    )

    if touched_level and bearish_rejection:
        return (
            "PUT",
            "Resistance Rejection: اختبار منطقة مقاومة متعددة اللمسات ورفض سعري"
        )

    return None

def strategy_trend_pullback(
    candles,
    trend,
    average_atr=None,
    current_adx=None,
    plus_di=None,
    minus_di=None
):
    """
    اتجاه قوي + تصحيح منظم + استئناف مؤكد.
    """

    if trend not in ("UP", "DOWN"):
        return None

    closes = [c["close"] for c in candles]

    if len(candles) < 55:
        return None

    current = candles[-1]
    previous = candles[-2]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    current_rsi = rsi(closes)
    slope20 = ema_slope(closes, 20)
    structure = market_structure(candles)

    if (
        ema20 is None
        or ema50 is None
        or current_rsi is None
        or average_atr is None
        or slope20 is None
        or current_adx is None
    ):
        return None

    if current_adx < TREND_ADX_MIN:
        return None

    metrics = candle_metrics(current)

    if not metrics:
        return None

    if metrics["body_ratio"] < 0.40:
        return None

    if metrics["range"] < average_atr * 0.50:
        return None

    if metrics["range"] > average_atr * 2.0:
        return None

    if trend == "UP":
        if structure != "UP" or slope20 <= 0:
            return None

        if plus_di is None or minus_di is None:
            return None

        if plus_di <= minus_di:
            return None

        # يجب أن يكون هناك تصحيح سابق باتجاه EMA20 أو EMA50.
        pullback_candles = candles[-5:-1]

        pullback = any(
            candle["low"] <= ema20 + average_atr * 0.20
            and candle["low"] >= ema50 - average_atr * 0.65
            for candle in pullback_candles
        )

        # لا نريد تصحيحاً كسر EMA50 بقوة.
        invalidated_pullback = any(
            candle["close"] < ema50 - average_atr * 0.35
            for candle in pullback_candles
        )

        recovery = (
            current["close"] > current["open"]
            and current["close"] > ema20
            and current["close"] > previous["high"]
            and metrics["close_position"] >= 0.72
            and current_rsi >= 50
            and current_rsi <= 72
        )

        if pullback and not invalidated_pullback and recovery:
            return (
                "CALL",
                "Trend Pullback: اتجاه صاعد قوي وتصحيح منظم واستئناف مؤكد"
            )

    if trend == "DOWN":
        if structure != "DOWN" or slope20 >= 0:
            return None

        if plus_di is None or minus_di is None:
            return None

        if minus_di <= plus_di:
            return None

        pullback_candles = candles[-5:-1]

        pullback = any(
            candle["high"] >= ema20 - average_atr * 0.20
            and candle["high"] <= ema50 + average_atr * 0.65
            for candle in pullback_candles
        )

        invalidated_pullback = any(
            candle["close"] > ema50 + average_atr * 0.35
            for candle in pullback_candles
        )

        recovery = (
            current["close"] < current["open"]
            and current["close"] < ema20
            and current["close"] < previous["low"]
            and metrics["close_position"] <= 0.28
            and current_rsi >= 28
            and current_rsi <= 50
        )

        if pullback and not invalidated_pullback and recovery:
            return (
                "PUT",
                "Trend Pullback: اتجاه هابط قوي وتصحيح منظم واستئناف مؤكد"
            )

    return None

def strategy_breakout_retest(candles, average_atr):
    if len(candles) < 35 or average_atr is None:
        return None

    previous = candles[-2]
    current = candles[-1]

    reference = candles[-32:-2]

    if len(reference) < 20:
        return None

    resistance = max(c["high"] for c in reference)
    support = min(c["low"] for c in reference)

    tolerance = average_atr * 0.22
    breakout_buffer = average_atr * 0.10

    previous_metrics = candle_metrics(previous)
    current_metrics = candle_metrics(current)

    if not previous_metrics or not current_metrics:
        return None

    bullish_retest = (
        previous["close"] > resistance + breakout_buffer
        and previous["close"] > previous["open"]
        and previous_metrics["body_ratio"] >= 0.45
        and current["low"] <= resistance + tolerance
        and current["low"] >= resistance - average_atr * 0.30
        and current["close"] > resistance + average_atr * 0.03
        and current["close"] > current["open"]
        and current_metrics["close_position"] >= 0.65
        and current_metrics["body_ratio"] >= 0.25
    )

    bearish_retest = (
        previous["close"] < support - breakout_buffer
        and previous["close"] < previous["open"]
        and previous_metrics["body_ratio"] >= 0.45
        and current["high"] >= support - tolerance
        and current["high"] <= support + average_atr * 0.30
        and current["close"] < support - average_atr * 0.03
        and current["close"] < current["open"]
        and current_metrics["close_position"] <= 0.35
        and current_metrics["body_ratio"] >= 0.25
    )

    if bullish_retest:
        return (
            "CALL",
            "Breakout Retest: اختراق قوي وإعادة اختبار ناجحة للمقاومة"
        )

    if bearish_retest:
        return (
            "PUT",
            "Breakout Retest: كسر قوي وإعادة اختبار ناجحة للدعم"
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

    strategy_count = len(selected)

    if strategy_count >= 2:
        score += min(12, (strategy_count - 1) * 6)

    score += metrics["body_ratio"] * 10

    if direction == "CALL":
        score += metrics["close_position"] * 8
        wick_ratio = (
            metrics["lower_wick"] / metrics["range"]
            if metrics["range"] > 0 else 0
        )
    else:
        score += (1 - metrics["close_position"]) * 8
        wick_ratio = (
            metrics["upper_wick"] / metrics["range"]
            if metrics["range"] > 0 else 0
        )

    if wick_ratio >= 0.25:
        score += 2

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
        elif range_ratio < 0.45:
            score -= 2

    return round(max(0, min(95, score)), 1)

def select_trade_duration(signal):
    """
    Experimental rule-based duration selection.
    Not an AI model or a win probability.
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
            average_atr,
            current_adx,
            plus_di,
            minus_di
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

    # لا نرسل اتجاهين متضادين للسوق نفسه.
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
        "market_structure": market_structure(candles),
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