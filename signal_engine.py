
import logging
import time
from math import isfinite

logger = logging.getLogger(__name__)

MIN_CANDLES = 60
EMA_LONG_PERIOD = 200
ADX_PERIOD = 14
SIGNAL_COOLDOWN_SECONDS = 3000

# Support / Resistance
SR_LOOKBACK = 60
SR_CLUSTER_ATR = 0.35
SR_MIN_TOUCHES = 2
SR_MIN_TOUCH_GAP = 3
SR_MIN_OPPOSING_ROOM_ATR = 0.80

# Trend
TREND_ADX_MIN = 20
TREND_SLOPE_LOOKBACK = 5

# Trend Pullback
PULLBACK_IMPULSE_LOOKBACK = 6
PULLBACK_MIN_IMPULSE_ATR = 0.35
PULLBACK_MIN_DIRECTIONAL_CANDLES = 2
PULLBACK_MAX_EMA_EXTENSION_ATR = 0.90

# Existing strategy parameters
LIQUIDITY_LOOKBACK = 20
LIQUIDITY_SWEEP_ATR = 0.08
MSS_LOOKBACK = 25
MSS_MIN_BODY_RATIO = 0.40
FVG_MIN_SIZE_ATR = 0.12
FVG_MAX_AGE = 8
VWAP_LOOKBACK = 30
VWAP_MIN_DISTANCE_ATR = 0.80
SQUEEZE_LOOKBACK = 20
SQUEEZE_HISTORY = 60
SQUEEZE_BREAKOUT_ATR = 0.08
DIVERGENCE_LOOKBACK = 35
DIVERGENCE_MIN_RSI_GAP = 4

# Order Block
OB_LOOKBACK = 35
OB_DISPLACEMENT_ATR = 1.20
OB_MIN_BODY_RATIO = 0.55
OB_BOS_LOOKBACK = 12
OB_RETEST_MAX_AGE = 8
OB_ZONE_TOLERANCE_ATR = 0.10
OB_REACTION_BODY_RATIO = 0.25
OB_REACTION_CLOSE_POSITION = 0.60

_sent = {}


def valid_candles(candles):
    if not isinstance(candles, list):
        return []

    result = []

    for candle in candles:
        if not isinstance(candle, dict):
            continue

        try:
            o, h, l, c = (
                float(candle[k])
                for k in ("open", "high", "low", "close")
            )

            if not all(isfinite(x) for x in (o, h, l, c)):
                continue

            if min(o, h, l, c) <= 0:
                continue

            if h < max(o, c, l) or l > min(o, c, h):
                continue

            item = {
                **candle,
                "open": o,
                "high": h,
                "low": l,
                "close": c
            }

            try:
                volume = float(candle.get("volume"))

                if isfinite(volume) and volume > 0:
                    item["volume"] = volume
                else:
                    item.pop("volume", None)

            except (TypeError, ValueError):
                item.pop("volume", None)

            result.append(item)

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

        ranges.append(
            max(
                current["high"] - current["low"],
                abs(current["high"] - previous_close),
                abs(current["low"] - previous_close)
            )
        )

    return ranges


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = true_ranges(candles)

    return sum(ranges[-period:]) / period


def adx(candles, period=ADX_PERIOD):
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

        plus_dm.append(
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus_dm.append(
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

        trs.append(
            max(
                current["high"] - current["low"],
                abs(current["high"] - previous["close"]),
                abs(current["low"] - previous["close"])
            )
        )

    smooth_tr = sum(trs[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])

    def calculate_dx():
        if smooth_tr <= 0:
            return 0.0, 0.0, 0.0

        plus_di = 100 * smooth_plus / smooth_tr
        minus_di = 100 * smooth_minus / smooth_tr

        denominator = plus_di + minus_di

        dx = (
            100 * abs(plus_di - minus_di) / denominator
            if denominator > 0
            else 0.0
        )

        return dx, plus_di, minus_di

    dx_values = []

    first_dx, plus_di, minus_di = calculate_dx()
    dx_values.append(first_dx)

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

        dx, plus_di, minus_di = calculate_dx()
        dx_values.append(dx)

    if len(dx_values) < period:
        return None, None, None

    current_adx = sum(dx_values[:period]) / period

    for value in dx_values[period:]:
        current_adx = (
            current_adx * (period - 1) + value
        ) / period

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

    if current_high > previous_high and current_low > previous_low:
        return "UP"

    if current_high < previous_high and current_low < previous_low:
        return "DOWN"

    return "MIXED"


def ema_slope(values, period=20, lookback=TREND_SLOPE_LOOKBACK):
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

    if (
        bullish_stack
        and price > ema20
        and slope20 > 0
        and structure == "UP"
    ):
        return "UP", ema20, ema50, ema200

    if (
        bearish_stack
        and price < ema20
        and slope20 < 0
        and structure == "DOWN"
    ):
        return "DOWN", ema20, ema50, ema200

    return "SIDEWAYS", ema20, ema50, ema200


def find_clustered_level(pivots, price, average_atr, direction):
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
            (pi, pl)
            for pi, pl in pivots
            if abs(pl - level) <= tolerance
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

        candidates.append({
            "level": center,
            "touches": len(distinct),
            "distance": abs(price - center)
        })

    if not candidates:
        return None

    candidates.sort(key=lambda item: item["distance"])

    return candidates[0]


def find_support_resistance(candles, lookback=SR_LOOKBACK):
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
        lows, price, average_atr, "SUPPORT"
    )

    resistance_data = find_clustered_level(
        highs, price, average_atr, "RESISTANCE"
    )

    return (
        support_data["level"] if support_data else None,
        resistance_data["level"] if resistance_data else None
    )


def has_opposing_level_room(price, opposing_level, average_atr, direction):
    if opposing_level is None:
        return True

    if average_atr is None or average_atr <= 0:
        return False

    if direction == "CALL":
        room = opposing_level - price

    elif direction == "PUT":
        room = price - opposing_level

    else:
        return False

    return room >= average_atr * SR_MIN_OPPOSING_ROOM_ATR


# ============================================================
# ORIGINAL STRATEGIES
# ============================================================

def strategy_support_rejection(candles, support, resistance, average_atr):
    if support is None or average_atr is None or len(candles) < 3:
        return None

    current = candles[-1]
    previous = candles[-2]

    metrics = candle_metrics(current)

    if not metrics:
        return None

    tolerance = average_atr * 0.25
    breakout_buffer = average_atr * 0.08

    false_breakout = (
        previous["low"] < support - breakout_buffer
        and previous["close"] < support
        and current["close"] > support + breakout_buffer
        and current["close"] > current["open"]
        and metrics["body_ratio"] >= 0.35
        and metrics["close_position"] >= 0.70
    )

    bullish_rejection = (
        current["close"] > current["open"]
        and current["close"] > support + average_atr * 0.03
        and metrics["lower_wick"] >= metrics["body"] * 1.3
        and metrics["lower_wick"] >= metrics["range"] * 0.28
        and metrics["close_position"] >= 0.68
        and metrics["body_ratio"] >= 0.18
    )

    if false_breakout or (
        current["low"] <= support + tolerance
        and current["low"] >= support - average_atr * 0.35
        and bullish_rejection
    ):
        if not has_opposing_level_room(
            current["close"], resistance, average_atr, "CALL"
        ):
            return None

        return (
            "CALL",
            "Support Rejection: اختبار الدعم ورفض سعري صاعد"
        )

    return None


def strategy_resistance_rejection(candles, support, resistance, average_atr):
    if resistance is None or average_atr is None or len(candles) < 3:
        return None

    current = candles[-1]
    previous = candles[-2]

    metrics = candle_metrics(current)

    if not metrics:
        return None

    tolerance = average_atr * 0.25
    breakout_buffer = average_atr * 0.08

    false_breakout = (
        previous["high"] > resistance + breakout_buffer
        and previous["close"] > resistance
        and current["close"] < resistance - breakout_buffer
        and current["close"] < current["open"]
        and metrics["body_ratio"] >= 0.35
        and metrics["close_position"] <= 0.30
    )

    bearish_rejection = (
        current["close"] < current["open"]
        and current["close"] < resistance - average_atr * 0.03
        and metrics["upper_wick"] >= metrics["body"] * 1.3
        and metrics["upper_wick"] >= metrics["range"] * 0.28
        and metrics["close_position"] <= 0.32
        and metrics["body_ratio"] >= 0.18
    )

    if false_breakout or (
        current["high"] >= resistance - tolerance
        and current["high"] <= resistance + average_atr * 0.35
        and bearish_rejection
    ):
        if not has_opposing_level_room(
            current["close"], support, average_atr, "PUT"
        ):
            return None

        return (
            "PUT",
            "Resistance Rejection: اختبار المقاومة ورفض سعري هابط"
        )

    return None


def check_trend_impulse(candles, direction, average_atr):
    if average_atr is None or average_atr <= 0:
        return False

    impulse_candles = candles[
        -(PULLBACK_IMPULSE_LOOKBACK + 5):-5
    ]

    if len(impulse_candles) != PULLBACK_IMPULSE_LOOKBACK:
        return False

    first = impulse_candles[0]
    last = impulse_candles[-1]

    if direction == "UP":
        net_move = last["close"] - first["open"]
    else:
        net_move = first["open"] - last["close"]

    if net_move < average_atr * PULLBACK_MIN_IMPULSE_ATR:
        return False

    if direction == "UP":
        directional_count = sum(
            1 for c in impulse_candles
            if c["close"] > c["open"]
        )
    else:
        directional_count = sum(
            1 for c in impulse_candles
            if c["close"] < c["open"]
        )

    return directional_count >= PULLBACK_MIN_DIRECTIONAL_CANDLES


def strategy_trend_pullback(
    candles,
    trend,
    average_atr=None,
    current_adx=None,
    plus_di=None,
    minus_di=None
):
    if len(candles) < 60 or average_atr is None or average_atr <= 0:
        return None

    if (
        current_adx is None
        or current_adx < TREND_ADX_MIN
        or plus_di is None
        or minus_di is None
    ):
        return None

    closes = [c["close"] for c in candles]

    current = candles[-1]
    previous = candles[-2]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    current_rsi = rsi(closes)
    slope20 = ema_slope(closes, 20)

    if None in (ema20, ema50, current_rsi, slope20):
        return None

    bullish_bias = (
        ema20 > ema50
        and slope20 > 0
        and plus_di > minus_di
        and current["close"] > ema20 - average_atr * 0.15
    )

    bearish_bias = (
        ema20 < ema50
        and slope20 < 0
        and minus_di > plus_di
        and current["close"] < ema20 + average_atr * 0.15
    )

    if bullish_bias == bearish_bias:
        return None

    direction = "UP" if bullish_bias else "DOWN"

    metrics = candle_metrics(current)

    if not metrics:
        return None

    if (
        metrics["body_ratio"] < 0.35
        or metrics["range"] < average_atr * 0.40
        or metrics["range"] > average_atr * 2.20
    ):
        return None

    if abs(current["close"] - ema20) / average_atr > PULLBACK_MAX_EMA_EXTENSION_ATR:
        return None

    if not check_trend_impulse(candles, direction, average_atr):
        return None

    pullback_candles = candles[-5:-1]

    if direction == "UP":
        pullback = any(
            c["low"] <= ema20 + average_atr * 0.35
            and c["low"] >= ema50 - average_atr * 0.80
            for c in pullback_candles
        )

        invalidated = any(
            c["close"] < ema50 - average_atr * 0.50
            for c in pullback_candles
        )

        recovery = (
            current["close"] > current["open"]
            and current["close"] > ema20
            and metrics["close_position"] >= 0.62
            and (
                current["close"] > previous["high"]
                or current["close"] > previous["close"] + average_atr * 0.03
            )
            and 48 <= current_rsi <= 75
        )

        if pullback and not invalidated and recovery:
            return (
                "CALL",
                "Trend Pullback: اتجاه صاعد وتصحيح واستئناف مؤكد"
            )

    else:
        pullback = any(
            c["high"] >= ema20 - average_atr * 0.35
            and c["high"] <= ema50 + average_atr * 0.80
            for c in pullback_candles
        )

        invalidated = any(
            c["close"] > ema50 + average_atr * 0.50
            for c in pullback_candles
        )

        recovery = (
            current["close"] < current["open"]
            and current["close"] < ema20
            and metrics["close_position"] <= 0.38
            and (
                current["close"] < previous["low"]
                or current["close"] < previous["close"] - average_atr * 0.03
            )
            and 25 <= current_rsi <= 52
        )

        if pullback and not invalidated and recovery:
            return (
                "PUT",
                "Trend Pullback: اتجاه هابط وتصحيح واستئناف مؤكد"
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

    bullish = (
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

    bearish = (
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

    if bullish:
        return (
            "CALL",
            "Breakout Retest: اختراق وإعادة اختبار ناجحة"
        )

    if bearish:
        return (
            "PUT",
            "Breakout Retest: كسر وإعادة اختبار ناجحة"
        )

    return None


def strategy_liquidity_sweep(candles, average_atr):
    if len(candles) < LIQUIDITY_LOOKBACK + 3 or not average_atr:
        return None

    current = candles[-1]

    previous_levels = candles[
        -(LIQUIDITY_LOOKBACK + 1):-1
    ]

    recent_high = max(c["high"] for c in previous_levels)
    recent_low = min(c["low"] for c in previous_levels)

    metrics = candle_metrics(current)

    if not metrics:
        return None

    buffer = average_atr * LIQUIDITY_SWEEP_ATR

    bullish_sweep = (
        current["low"] < recent_low - buffer
        and current["close"] > recent_low
        and current["close"] > current["open"]
        and metrics["lower_wick"] >= metrics["body"] * 0.8
        and metrics["close_position"] >= 0.62
    )

    bearish_sweep = (
        current["high"] > recent_high + buffer
        and current["close"] < recent_high
        and current["close"] < current["open"]
        and metrics["upper_wick"] >= metrics["body"] * 0.8
        and metrics["close_position"] <= 0.38
    )

    if bullish_sweep:
        return (
            "CALL",
            "Liquidity Sweep: سحب سيولة أسفل القاع والعودة فوقه"
        )

    if bearish_sweep:
        return (
            "PUT",
            "Liquidity Sweep: سحب سيولة أعلى القمة والعودة أسفلها"
        )

    return None


def strategy_market_structure_shift(candles, average_atr):
    if len(candles) < MSS_LOOKBACK + 5 or not average_atr:
        return None

    current = candles[-1]
    history = candles[-(MSS_LOOKBACK + 1):-1]

    metrics = candle_metrics(current)

    if not metrics or metrics["body_ratio"] < MSS_MIN_BODY_RATIO:
        return None

    pivot_highs, pivot_lows = find_pivots(history)

    if not pivot_highs or not pivot_lows:
        return None

    last_swing_high = pivot_highs[-1][1]
    last_swing_low = pivot_lows[-1][1]

    buffer = average_atr * 0.05

    bullish_shift = (
        current["close"] > last_swing_high + buffer
        and current["close"] > current["open"]
        and metrics["close_position"] >= 0.68
    )

    bearish_shift = (
        current["close"] < last_swing_low - buffer
        and current["close"] < current["open"]
        and metrics["close_position"] <= 0.32
    )

    if bullish_shift:
        return (
            "CALL",
            "Market Structure Shift: كسر هيكل هابط باتجاه صاعد"
        )

    if bearish_shift:
        return (
            "PUT",
            "Market Structure Shift: كسر هيكل صاعد باتجاه هابط"
        )

    return None


def strategy_fair_value_gap(candles, average_atr):
    if len(candles) < FVG_MAX_AGE + 5 or not average_atr:
        return None

    current = candles[-1]
    metrics = candle_metrics(current)

    if not metrics:
        return None

    for age in range(1, FVG_MAX_AGE + 1):
        third_index = len(candles) - age - 1

        if third_index < 2:
            continue

        first = candles[third_index - 2]
        middle = candles[third_index - 1]
        third = candles[third_index]

        middle_metrics = candle_metrics(middle)

        if (
            not middle_metrics
            or middle_metrics["body_ratio"] < 0.50
        ):
            continue

        bullish_gap = third["low"] - first["high"]

        if (
            bullish_gap >= average_atr * FVG_MIN_SIZE_ATR
            and middle["close"] > middle["open"]
        ):
            gap_low = first["high"]
            gap_high = third["low"]

            touched = (
                current["low"] <= gap_high
                and current["high"] >= gap_low
            )

            bullish_reaction = (
                current["close"] > current["open"]
                and current["close"] > gap_low
                and metrics["close_position"] >= 0.58
            )

            if touched and bullish_reaction:
                return (
                    "CALL",
                    "Fair Value Gap: إعادة اختبار فجوة صاعدة وارتداد"
                )

        bearish_gap = first["low"] - third["high"]

        if (
            bearish_gap >= average_atr * FVG_MIN_SIZE_ATR
            and middle["close"] < middle["open"]
        ):
            gap_low = third["high"]
            gap_high = first["low"]

            touched = (
                current["high"] >= gap_low
                and current["low"] <= gap_high
            )

            bearish_reaction = (
                current["close"] < current["open"]
                and current["close"] < gap_high
                and metrics["close_position"] <= 0.42
            )

            if touched and bearish_reaction:
                return (
                    "PUT",
                    "Fair Value Gap: إعادة اختبار فجوة هابطة ورفض"
                )

    return None


def strategy_vwap_mean_reversion(candles, average_atr):
    if len(candles) < VWAP_LOOKBACK + 2 or not average_atr:
        return None

    window = candles[-VWAP_LOOKBACK:]

    if any(
        "volume" not in candle or candle["volume"] <= 0
        for candle in window
    ):
        return None

    total_volume = sum(c["volume"] for c in window)

    if total_volume <= 0:
        return None

    weighted_sum = sum(
        (
            (c["high"] + c["low"] + c["close"]) / 3
        ) * c["volume"]
        for c in window
    )

    vwap_value = weighted_sum / total_volume

    current = candles[-1]
    previous = candles[-2]

    metrics = candle_metrics(current)

    if not metrics:
        return None

    bullish_reversion = (
        previous["close"] < vwap_value - average_atr * VWAP_MIN_DISTANCE_ATR
        and current["close"] > current["open"]
        and current["close"] > previous["close"]
        and current["close"] > vwap_value - average_atr * 0.45
        and metrics["close_position"] >= 0.60
    )

    bearish_reversion = (
        previous["close"] > vwap_value + average_atr * VWAP_MIN_DISTANCE_ATR
        and current["close"] < current["open"]
        and current["close"] < previous["close"]
        and current["close"] < vwap_value + average_atr * 0.45
        and metrics["close_position"] <= 0.40
    )

    if bullish_reversion:
        return (
            "CALL",
            "VWAP Mean Reversion: عودة صاعدة باتجاه VWAP"
        )

    if bearish_reversion:
        return (
            "PUT",
            "VWAP Mean Reversion: عودة هابطة باتجاه VWAP"
        )

    return None


def bollinger_width(candles, period=20):
    if len(candles) < period:
        return None

    closes = [
        c["close"]
        for c in candles[-period:]
    ]

    middle = sum(closes) / period

    if middle <= 0:
        return None

    variance = sum(
        (value - middle) ** 2
        for value in closes
    ) / period

    deviation = variance ** 0.5

    return (
        (middle + 2 * deviation)
        - (middle - 2 * deviation)
    ) / middle


def strategy_volatility_squeeze_breakout(candles, average_atr):
    required = SQUEEZE_HISTORY + SQUEEZE_LOOKBACK + 2

    if len(candles) < required or not average_atr:
        return None

    widths = []

    for end in range(
        len(candles) - SQUEEZE_HISTORY - 1,
        len(candles) - 1
    ):
        width = bollinger_width(
            candles[:end + 1],
            SQUEEZE_LOOKBACK
        )

        if width is not None:
            widths.append(width)

    if len(widths) < 30:
        return None

    current_width = bollinger_width(
        candles[:-1],
        SQUEEZE_LOOKBACK
    )

    if current_width is None:
        return None

    sorted_widths = sorted(widths)

    lower_quartile = sorted_widths[
        max(0, int(len(sorted_widths) * 0.25) - 1)
    ]

    if current_width > lower_quartile:
        return None

    current = candles[-1]

    previous_range = candles[
        -(SQUEEZE_LOOKBACK + 1):-1
    ]

    range_high = max(c["high"] for c in previous_range)
    range_low = min(c["low"] for c in previous_range)

    metrics = candle_metrics(current)

    if not metrics:
        return None

    bullish = (
        current["close"] > range_high + average_atr * SQUEEZE_BREAKOUT_ATR
        and current["close"] > current["open"]
        and metrics["body_ratio"] >= 0.50
        and metrics["close_position"] >= 0.70
        and metrics["range"] <= average_atr * 2.5
    )

    bearish = (
        current["close"] < range_low - average_atr * SQUEEZE_BREAKOUT_ATR
        and current["close"] < current["open"]
        and metrics["body_ratio"] >= 0.50
        and metrics["close_position"] <= 0.30
        and metrics["range"] <= average_atr * 2.5
    )

    if bullish:
        return (
            "CALL",
            "Volatility Squeeze Breakout: اختراق صاعد بعد انضغاط التذبذب"
        )

    if bearish:
        return (
            "PUT",
            "Volatility Squeeze Breakout: اختراق هابط بعد انضغاط التذبذب"
        )

    return None


def strategy_momentum_divergence(candles):
    if len(candles) < DIVERGENCE_LOOKBACK + 20:
        return None

    history = candles[-DIVERGENCE_LOOKBACK:]

    closes = [c["close"] for c in history]

    rsi_values = [
        rsi(closes[:i + 1])
        for i in range(len(history))
    ]

    price_highs, price_lows = find_pivots(history)

    valid_highs = [
        (i, value)
        for i, value in price_highs
        if rsi_values[i] is not None
    ]

    valid_lows = [
        (i, value)
        for i, value in price_lows
        if rsi_values[i] is not None
    ]

    current = history[-1]
    metrics = candle_metrics(current)

    if not metrics:
        return None

    if len(valid_lows) >= 2:
        (old_i, old_low), (new_i, new_low) = valid_lows[-2:]

        old_rsi = rsi_values[old_i]
        new_rsi = rsi_values[new_i]

        if (
            new_low < old_low
            and new_rsi > old_rsi + DIVERGENCE_MIN_RSI_GAP
            and new_i >= len(history) - 5
            and current["close"] > current["open"]
            and metrics["close_position"] >= 0.58
        ):
            return (
                "CALL",
                "Momentum Divergence: قاع سعري أدنى مع RSI أعلى"
            )

    if len(valid_highs) >= 2:
        (old_i, old_high), (new_i, new_high) = valid_highs[-2:]

        old_rsi = rsi_values[old_i]
        new_rsi = rsi_values[new_i]

        if (
            new_high > old_high
            and new_rsi < old_rsi - DIVERGENCE_MIN_RSI_GAP
            and new_i >= len(history) - 5
            and current["close"] < current["open"]
            and metrics["close_position"] <= 0.42
        ):
            return (
                "PUT",
                "Momentum Divergence: قمة سعرية أعلى مع RSI أدنى"
            )

    return None


# ============================================================
# Order Block
# ============================================================

def strategy_order_block(candles, average_atr):
    """
    Causal Order Block detection.

    1. Identify a recent opposing candle.
    2. Require displacement measured against ATR.
    3. Require BOS beyond a level formed before displacement.
    4. Wait for a retest of the original wick-to-wick zone.
    5. Current candle may be the first retest.
    6. Require a directional reaction before emitting a signal.

    Only completed candles at or before the current candle are used.
    """

    minimum = max(
        OB_LOOKBACK,
        OB_BOS_LOOKBACK + 5
    )

    if (
        average_atr is None
        or average_atr <= 0
        or len(candles) < minimum + 3
    ):
        return None

    current_index = len(candles) - 1
    current = candles[current_index]

    current_metrics = candle_metrics(current)

    if not current_metrics:
        return None

    # Displacement must be before the current retest/reaction candle.
    first_candidate = max(
        2,
        current_index - OB_RETEST_MAX_AGE - 1
    )

    last_candidate = current_index - 1

    for displacement_index in range(
        last_candidate,
        first_candidate - 1,
        -1
    ):
        displacement = candles[displacement_index]

        displacement_metrics = candle_metrics(displacement)

        if not displacement_metrics:
            continue

        if (
            displacement_metrics["range"]
            < average_atr * OB_DISPLACEMENT_ATR
            or displacement_metrics["body_ratio"]
            < OB_MIN_BODY_RATIO
        ):
            continue

        # BOS reference strictly before displacement.
        bos_start = max(
            0,
            displacement_index - OB_BOS_LOOKBACK
        )

        bos_history = candles[
            bos_start:displacement_index
        ]

        if len(bos_history) < 5:
            continue

        prior_high = max(
            c["high"]
            for c in bos_history
        )

        prior_low = min(
            c["low"]
            for c in bos_history
        )

        bullish_displacement = (
            displacement["close"] > displacement["open"]
            and displacement["close"] > prior_high
        )

        bearish_displacement = (
            displacement["close"] < displacement["open"]
            and displacement["close"] < prior_low
        )

        if not bullish_displacement and not bearish_displacement:
            continue

        direction = (
            "CALL"
            if bullish_displacement
            else "PUT"
        )

        # Find the last opposing candle before displacement.
        ob_index = None

        search_start = max(
            0,
            displacement_index - 4
        )

        for candidate_index in range(
            displacement_index - 1,
            search_start - 1,
            -1
        ):
            candidate = candles[candidate_index]

            if (
                direction == "CALL"
                and candidate["close"] < candidate["open"]
            ):
                ob_index = candidate_index
                break

            if (
                direction == "PUT"
                and candidate["close"] > candidate["open"]
            ):
                ob_index = candidate_index
                break

        if ob_index is None:
            continue

        ob_candle = candles[ob_index]

        # Wick-to-wick Order Block zone.
        zone_low = ob_candle["low"]
        zone_high = ob_candle["high"]

        if zone_high <= zone_low:
            continue

        # Reject a block invalidated after formation.
        post_ob = candles[
            ob_index + 1:current_index
        ]

        if direction == "CALL":
            invalidated = any(
                c["close"] < zone_low
                for c in post_ob
            )
        else:
            invalidated = any(
                c["close"] > zone_high
                for c in post_ob
            )

        if invalidated:
            continue

        # The current candle may be the first retest.
        tolerance = (
            average_atr * OB_ZONE_TOLERANCE_ATR
        )

        current_touches = (
            current["low"] <= zone_high + tolerance
            and current["high"] >= zone_low - tolerance
        )

        if not current_touches:
            continue

        midpoint = (
            zone_low + zone_high
        ) / 2

        if direction == "CALL":
            reaction = (
                current["close"] > current["open"]
                and current["close"] > midpoint
                and current_metrics["body_ratio"]
                >= OB_REACTION_BODY_RATIO
                and current_metrics["close_position"]
                >= OB_REACTION_CLOSE_POSITION
            )

            if reaction:
                return (
                    "CALL",
                    "Order Block: إعادة اختبار كتلة طلب بعد BOS وارتداد صاعد"
                )

        else:
            reaction = (
                current["close"] < current["open"]
                and current["close"] < midpoint
                and current_metrics["body_ratio"]
                >= OB_REACTION_BODY_RATIO
                and current_metrics["close_position"]
                <= (1 - OB_REACTION_CLOSE_POSITION)
            )

            if reaction:
                return (
                    "PUT",
                    "Order Block: إعادة اختبار كتلة عرض بعد BOS ورفض هابط"
                )

    return None


# ============================================================
# Signal score
# ============================================================

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
    score = 45.0

    strategy_count = len(selected)

    if strategy_count >= 2:
        score += min(
            12,
            (strategy_count - 1) * 6
        )

    score += metrics["body_ratio"] * 10

    if direction == "CALL":
        score += metrics["close_position"] * 8

        wick_ratio = (
            metrics["lower_wick"] / metrics["range"]
            if metrics["range"] > 0
            else 0
        )

    else:
        score += (
            1 - metrics["close_position"]
        ) * 8

        wick_ratio = (
            metrics["upper_wick"] / metrics["range"]
            if metrics["range"] > 0
            else 0
        )

    if wick_ratio >= 0.25:
        score += 2

    if (
        direction == "CALL" and trend == "UP"
    ) or (
        direction == "PUT" and trend == "DOWN"
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
        range_ratio = (
            metrics["range"] / average_atr
        )

        if 0.65 <= range_ratio <= 1.8:
            score += 3

        elif range_ratio > 2.3:
            score -= 5

        elif range_ratio < 0.45:
            score -= 2

    return round(
        max(0, min(95, score)),
        1
    )


def select_trade_duration(signal):
    direction = signal.get("direction")
    trend = signal.get("trend")

    adx_value = signal.get("adx")
    body_ratio = signal.get("body_ratio")

    range_ratio = signal.get("range_ratio")
    rsi_value = signal.get("rsi")

    strategies = signal.get(
        "matching_strategies",
        []
    )

    trend_aligned = (
        direction == "CALL" and trend == "UP"
    ) or (
        direction == "PUT" and trend == "DOWN"
    )

    continuation_signal = any(
        name in strategies
        for name in (
            "Trend Pullback",
            "Breakout Retest",
            "Market Structure Shift",
            "Volatility Squeeze Breakout"
        )
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

    if any(
        name in strategies
        for name in (
            "Support Rejection",
            "Resistance Rejection",
            "Liquidity Sweep",
            "Fair Value Gap",
            "VWAP Mean Reversion",
            "Momentum Divergence",
            "Order Block"
        )
    ):
        return (
            60,
            "إشارة ارتداد أو انعكاس؛ اختيار مدة أقصر للاختبار"
        )

    return (
        60,
        "شروط الاستمرار لمدة دقيقتين غير مكتملة"
    )


# ============================================================
# Main analysis
# ============================================================

STRATEGY_FUNCTIONS = (
    strategy_support_rejection,
    strategy_resistance_rejection,
    strategy_trend_pullback,
    strategy_breakout_retest,
    strategy_liquidity_sweep,
    strategy_market_structure_shift,
    strategy_fair_value_gap,
    strategy_vwap_mean_reversion,
    strategy_volatility_squeeze_breakout,
    strategy_momentum_divergence,
    strategy_order_block,
)


def analyze_signal(candles_1m, candles_5m=None, symbol=None):
    # All strategies use completed 1-minute candles supplied by scanner.
    candles = valid_candles(candles_1m)

    if len(candles) < MIN_CANDLES:
        return None

    closes = [
        c["close"]
        for c in candles
    ]

    current = candles[-1]
    price = current["close"]

    average_atr = atr(candles)

    if average_atr is None or average_atr <= 0:
        return None

    current_metrics = candle_metrics(current)

    if not current_metrics:
        return None

    if (
        current_metrics["range"] < average_atr * 0.35
        or current_metrics["range"] > average_atr * 3.0
    ):
        return None

    trend, ema20, ema50, ema200 = get_trend(candles)

    current_rsi = rsi(closes)

    current_adx, plus_di, minus_di = adx(candles)

    range_ratio = (
        current_metrics["range"] / average_atr
    )

    support, resistance = find_support_resistance(candles)

    strategy_results = [
        strategy_support_rejection(
            candles, support, resistance, average_atr
        ),

        strategy_resistance_rejection(
            candles, support, resistance, average_atr
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
            candles, average_atr
        ),

        strategy_liquidity_sweep(
            candles, average_atr
        ),

        strategy_market_structure_shift(
            candles, average_atr
        ),

        strategy_fair_value_gap(
            candles, average_atr
        ),

        strategy_vwap_mean_reversion(
            candles, average_atr
        ),

        strategy_volatility_squeeze_breakout(
            candles, average_atr
        ),

        strategy_momentum_divergence(
            candles
        ),

        strategy_order_block(
            candles, average_atr
        ),
    ]

    calls = []
    puts = []

    for result in strategy_results:
        if result is None:
            continue

        if len(result) == 2:
            direction, reason = result
            strategy_name = reason.split(":")[0]

        else:
            strategy_name, direction, reason = result

        if direction == "CALL":
            calls.append(
                (strategy_name, reason, direction)
            )

        elif direction == "PUT":
            puts.append(
                (strategy_name, reason, direction)
            )

    # Preserve the existing conservative conflict rule.
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

    direction = selected[0][2]

    matching_strategies = [
        item[0]
        for item in selected
    ]

    strategy_reasons = [
        item[1]
        for item in selected
    ]

    score = calculate_signal_score(
        selected,
        direction,
        trend,
        ema200,
        price,
        current_rsi,
        current_metrics,
        current_adx,
        plus_di,
        minus_di,
        average_atr
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
        "analysis_timeframe": "1m",
        "strategy_count": len(STRATEGY_FUNCTIONS),
        "volume_available": all(
            "volume" in c
            for c in candles[-VWAP_LOOKBACK:]
        )
    }

    selected_duration, duration_reason = (
        select_trade_duration(signal)
    )

    signal["selected_duration"] = selected_duration
    signal["duration_seconds"] = selected_duration
    signal["expiry_minutes"] = selected_duration // 60
    signal["duration_reason"] = duration_reason

    logger.info(
        "SIGNAL ANALYSIS | symbol=%s | direction=%s | strategies=%s | score=%.1f | duration=%s",
        symbol,
        direction,
        matching_strategies,
        score,
        selected_duration
    )

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

    if (
        previous_time is not None
        and now - previous_time < SIGNAL_COOLDOWN_SECONDS
    ):
        return False

    _sent[key] = now

    expired = [
        item
        for item, timestamp in _sent.items()
        if now - timestamp >= SIGNAL_COOLDOWN_SECONDS
    ]

    for item in expired:
        _sent.pop(item, None)

    return True
