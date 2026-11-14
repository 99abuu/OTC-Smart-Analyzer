
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# =========================================================
# MULTI-STRATEGY OTC ANALYZER
# 15 analysis modules + independent confirmation engine
# =========================================================

MIN_SCORE = 9
MIN_VOTES = 3

EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14

sent_signals = set()


# =========================================================
# BASIC DATA
# =========================================================

def candle_value(candle, key, default=0):
    try:
        return float(candle.get(key, default))
    except (TypeError, ValueError, AttributeError):
        return float(default)


def valid_candles(candles):
    if not isinstance(candles, list):
        return False

    for c in candles:
        if not isinstance(c, dict):
            return False

        for key in ("open", "high", "low", "close"):
            try:
                float(c[key])
            except (KeyError, TypeError, ValueError):
                return False

        if candle_value(c, "high") < candle_value(c, "low"):
            return False

    return True


def get_values(candles, key):
    return [candle_value(c, key) for c in candles]


# =========================================================
# EMA
# =========================================================

def ema(values, period):
    if len(values) < period or period <= 0:
        return []

    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    result = [None] * (period - 1)
    result.append(current)

    for value in values[period:]:
        current = (value - current) * multiplier + current
        result.append(current)

    return result


# =========================================================
# RSI
# =========================================================

def calculate_rsi(values, period=14):
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
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# =========================================================
# ATR
# =========================================================

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):
        high = candle_value(candles[i], "high")
        low = candle_value(candles[i], "low")
        previous_close = candle_value(candles[i - 1], "close")

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    current = sum(trs[:period]) / period

    for value in trs[period:]:
        current = ((current * (period - 1)) + value) / period

    return current


# =========================================================
# ADX
# =========================================================

def calculate_adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        high = candle_value(candles[i], "high")
        low = candle_value(candles[i], "low")
        prev_high = candle_value(candles[i - 1], "high")
        prev_low = candle_value(candles[i - 1], "low")
        prev_close = candle_value(candles[i - 1], "close")

        up = high - prev_high
        down = prev_low - low

        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)

        trs.append(max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        ))

    tr_s = sum(trs[:period])
    plus_s = sum(plus_dm[:period])
    minus_s = sum(minus_dm[:period])

    dx_values = []

    for i in range(period, len(trs)):
        if i > period:
            tr_s = tr_s - (tr_s / period) + trs[i]
            plus_s = plus_s - (plus_s / period) + plus_dm[i]
            minus_s = minus_s - (minus_s / period) + minus_dm[i]

        if tr_s <= 0:
            continue

        plus_di = 100 * plus_s / tr_s
        minus_di = 100 * minus_s / tr_s
        total = plus_di + minus_di

        dx = 0 if total == 0 else (
            100 * abs(plus_di - minus_di) / total
        )
        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx_value = sum(dx_values[:period]) / period

    for value in dx_values[period:]:
        adx_value = (
            (adx_value * (period - 1)) + value
        ) / period

    return adx_value


# =========================================================
# 15-MINUTE CANDLES FROM 5-MINUTE DATA
# =========================================================

def build_15m_candles(candles_5m):
    result = []

    # Use only complete groups of three 5-minute candles.
    usable = len(candles_5m) - (len(candles_5m) % 3)

    for i in range(0, usable, 3):
        group = candles_5m[i:i + 3]

        if len(group) != 3:
            continue

        result.append({
            "open": candle_value(group[0], "open"),
            "high": max(candle_value(c, "high") for c in group),
            "low": min(candle_value(c, "low") for c in group),
            "close": candle_value(group[-1], "close"),
            "volume": sum(candle_value(c, "volume") for c in group)
        })

    return result


# =========================================================
# MARKET TREND
# =========================================================

def determine_trend(candles):
    if len(candles) < 60:
        return "UNKNOWN"

    closes = get_values(candles, "close")
    fast = ema(closes, 20)
    slow = ema(closes, 50)

    if not fast or not slow:
        return "UNKNOWN"

    price = closes[-1]

    if fast[-1] is None or slow[-1] is None:
        return "UNKNOWN"

    if price > fast[-1] > slow[-1]:
        return "CALL"

    if price < fast[-1] < slow[-1]:
        return "PUT"

    return "SIDEWAYS"


# =========================================================
# SUPPORT AND RESISTANCE
# =========================================================

def support_resistance(candles, lookback=40):
    data = candles[-lookback:]

    if len(data) < 10:
        return None, None

    support = min(candle_value(c, "low") for c in data)
    resistance = max(candle_value(c, "high") for c in data)

    return support, resistance


# =========================================================
# MARKET STRUCTURE
# =========================================================

def market_structure(candles, lookback=30):
    if len(candles) < lookback + 5:
        return None

    data = candles[-lookback:]
    previous = candles[-lookback - 5:-5]

    current_high = max(candle_value(c, "high") for c in data)
    current_low = min(candle_value(c, "low") for c in data)

    previous_high = max(candle_value(c, "high") for c in previous)
    previous_low = min(candle_value(c, "low") for c in previous)

    close = candle_value(candles[-1], "close")

    if close > previous_high:
        return "CALL"

    if close < previous_low:
        return "PUT"

    if current_high < previous_high and current_low > previous_low:
        return "RANGE"

    return None


# =========================================================
# FALSE BREAKOUT / LIQUIDITY SWEEP
# =========================================================

def liquidity_sweep(candles, lookback=20):
    if len(candles) < lookback + 2:
        return None

    previous = candles[-lookback - 1:-1]
    current = candles[-1]

    old_high = max(candle_value(c, "high") for c in previous)
    old_low = min(candle_value(c, "low") for c in previous)

    high = candle_value(current, "high")
    low = candle_value(current, "low")
    close = candle_value(current, "close")

    if high > old_high and close < old_high:
        return "PUT"

    if low < old_low and close > old_low:
        return "CALL"

    return None


# =========================================================
# BREAKOUT AND RETEST
# =========================================================

def breakout_retest(candles, lookback=20):
    if len(candles) < lookback + 3:
        return None

    previous = candles[-lookback - 2:-2]
    confirmation = candles[-2]
    current = candles[-1]

    resistance = max(candle_value(c, "high") for c in previous)
    support = min(candle_value(c, "low") for c in previous)

    close_previous = candle_value(confirmation, "close")
    close_current = candle_value(current, "close")

    low = candle_value(current, "low")
    high = candle_value(current, "high")

    if close_previous > resistance and low <= resistance and close_current > resistance:
        return "CALL"

    if close_previous < support and high >= support and close_current < support:
        return "PUT"

    return None


# =========================================================
# SUPPLY AND DEMAND APPROXIMATION
# =========================================================

def supply_demand(candles):
    if len(candles) < 12:
        return None

    current = candles[-1]
    recent = candles[-8:-1]

    close = candle_value(current, "close")
    atr = calculate_atr(candles[-20:], 14)

    if atr is None or atr <= 0:
        return None

    for candle in recent:
        body = abs(
            candle_value(candle, "close") -
            candle_value(candle, "open")
        )

        if body >= atr * 1.2:
            if candle_value(candle, "close") > candle_value(candle, "open"):
                zone_low = candle_value(candle, "low")
                zone_high = candle_value(candle, "open")

                if zone_low <= close <= zone_high + atr * 0.15:
                    return "CALL"

            else:
                zone_low = candle_value(candle, "open")
                zone_high = candle_value(candle, "high")

                if zone_low - atr * 0.15 <= close <= zone_high:
                    return "PUT"

    return None


# =========================================================
# FAIR VALUE GAP
# =========================================================

def fair_value_gap(candles):
    if len(candles) < 3:
        return None

    a = candles[-3]
    c = candles[-1]
    close = candle_value(c, "close")

    a_high = candle_value(a, "high")
    a_low = candle_value(a, "low")
    c_high = candle_value(c, "high")
    c_low = candle_value(c, "low")

    if a_high < c_low:
        if a_high <= close <= c_low:
            return "CALL"

    if a_low > c_high:
        if c_high <= close <= a_low:
            return "PUT"

    return None


# =========================================================
# CANDLE PATTERNS
# =========================================================

def candle_pattern(candles):
    if len(candles) < 3:
        return None

    previous = candles[-2]
    current = candles[-1]

    po = candle_value(previous, "open")
    pc = candle_value(previous, "close")
    co = candle_value(current, "open")
    cc = candle_value(current, "close")
    ch = candle_value(current, "high")
    cl = candle_value(current, "low")

    body = abs(cc - co)
    total = max(ch - cl, 0.00000001)

    lower_wick = min(co, cc) - cl
    upper_wick = ch - max(co, cc)

    # Bullish engulfing
    if pc < po and cc > co and co <= pc and cc >= po:
        return "CALL"

    # Bearish engulfing
    if pc > po and cc < co and co >= pc and cc <= po:
        return "PUT"

    # Bullish pin bar
    if lower_wick >= body * 2 and lower_wick / total >= 0.50 and cc > co:
        return "CALL"

    # Bearish pin bar
    if upper_wick >= body * 2 and upper_wick / total >= 0.50 and cc < co:
        return "PUT"

    return None


# =========================================================
# MOMENTUM / RSI
# =========================================================

def momentum_signal(candles):
    closes = get_values(candles, "close")

    if len(closes) < 5:
        return None

    rsi = calculate_rsi(closes, RSI_PERIOD)

    if rsi is None:
        return None

    change = closes[-1] - closes[-4]

    if change > 0 and 50 < rsi < 68:
        return "CALL"

    if change < 0 and 32 < rsi < 50:
        return "PUT"

    return None


# =========================================================
# MACD
# =========================================================

def macd_signal(candles):
    closes = get_values(candles, "close")

    if len(closes) < 35:
        return None

    fast = ema(closes, 12)
    slow = ema(closes, 26)

    if not fast or not slow:
        return None

    macd_line = [
        (fast[i] - slow[i]) if fast[i] is not None and slow[i] is not None else None
        for i in range(len(closes))
    ]

    valid = [x for x in macd_line if x is not None]

    if len(valid) < 9:
        return None

    signal_line = ema(valid, 9)

    if not signal_line:
        return None

    if valid[-1] > signal_line[-1] and valid[-2] <= signal_line[-2]:
        return "CALL"

    if valid[-1] < signal_line[-1] and valid[-2] >= signal_line[-2]:
        return "PUT"

    return None


# =========================================================
# DIVERGENCE
# =========================================================

def divergence_signal(candles):
    if len(candles) < 35:
        return None

    closes = get_values(candles, "close")
    rsi_values = []

    for i in range(15, len(closes)):
        value = calculate_rsi(closes[:i + 1], 14)
        rsi_values.append(value)

    if len(rsi_values) < 12:
        return None

    old_price = closes[-12]
    new_price = closes[-1]
    old_rsi = rsi_values[-12]
    new_rsi = rsi_values[-1]

    if old_rsi is None or new_rsi is None:
        return None

    if new_price < old_price and new_rsi > old_rsi + 3:
        return "CALL"

    if new_price > old_price and new_rsi < old_rsi - 3:
        return "PUT"

    return None


# =========================================================
# VOLATILITY SQUEEZE
# =========================================================

def volatility_squeeze(candles):
    if len(candles) < 25:
        return None

    closes = get_values(candles, "close")
    recent = closes[-20:]

    mean = sum(recent) / len(recent)

    variance = sum((x - mean) ** 2 for x in recent) / len(recent)
    deviation = variance ** 0.5

    if mean == 0:
        return None

    bandwidth = (4 * deviation) / mean

    if bandwidth > 0.008:
        return None

    current = closes[-1]
    previous = closes[-2]

    if current > previous:
        return "CALL"

    if current < previous:
        return "PUT"

    return None


# =========================================================
# VOLUME CONFIRMATION
# =========================================================

def volume_signal(candles):
    if len(candles) < 21:
        return None

    volumes = [candle_value(c, "volume") for c in candles[-21:-1]]

    if not any(v > 0 for v in volumes):
        return None

    average = sum(volumes) / len(volumes)
    current_volume = candle_value(candles[-1], "volume")

    if average <= 0 or current_volume < average * 1.5:
        return None

    current = candles[-1]

    if candle_value(current, "close") > candle_value(current, "open"):
        return "CALL"

    if candle_value(current, "close") < candle_value(current, "open"):
        return "PUT"

    return None


# =========================================================
# EMA TREND CONFIRMATION
# =========================================================

def ema_signal(candles):
    closes = get_values(candles, "close")

    fast = ema(closes, 50)
    slow = ema(closes, 200)

    if not fast or not slow:
        return None

    if fast[-1] is None or slow[-1] is None:
        return None

    if closes[-1] > fast[-1] > slow[-1]:
        return "CALL"

    if closes[-1] < fast[-1] < slow[-1]:
        return "PUT"

    return None


# =========================================================
# PRICE QUALITY
# =========================================================

def candle_quality(candles):
    current = candles[-1]

    o = candle_value(current, "open")
    h = candle_value(current, "high")
    l = candle_value(current, "low")
    c = candle_value(current, "close")

    total = h - l

    if total <= 0:
        return None

    body_ratio = abs(c - o) / total
    close_position = (c - l) / total

    if body_ratio < 0.45:
        return None

    if c > o and close_position >= 0.70:
        return "CALL"

    if c < o and close_position <= 0.30:
        return "PUT"

    return None


# =========================================================
# PULLBACK RESUMPTION
# =========================================================

def pullback_signal(candles):
    if len(candles) < 30:
        return None

    closes = get_values(candles, "close")
    highs = get_values(candles, "high")
    lows = get_values(candles, "low")

    fast = ema(closes, 9)
    slow = ema(closes, 21)

    if not fast or not slow:
        return None

    if fast[-1] is None or slow[-1] is None:
        return None

    current = candles[-1]

    if fast[-1] > slow[-1] and candle_value(current, "close") > fast[-1]:
        touched = any(
            lows[i] <= fast[i]
            for i in range(max(0, len(candles) - 5), len(candles) - 1)
            if fast[i] is not None
        )
        if touched:
            return "CALL"

    if fast[-1] < slow[-1] and candle_value(current, "close") < fast[-1]:
        touched = any(
            highs[i] >= fast[i]
            for i in range(max(0, len(candles) - 5), len(candles) - 1)
            if fast[i] is not None
        )
        if touched:
            return "PUT"

    return None


# =========================================================
# SUPPORT / RESISTANCE REJECTION
# =========================================================

def sr_rejection(candles):
    if len(candles) < 25:
        return None

    support, resistance = support_resistance(candles[:-1], 20)

    if support is None or resistance is None:
        return None

    current = candles[-1]
    close = candle_value(current, "close")
    low = candle_value(current, "low")
    high = candle_value(current, "high")

    atr = calculate_atr(candles[-20:], 14)

    if atr is None or atr <= 0:
        return None

    if low <= support + atr * 0.15 and close > support:
        return "CALL"

    if high >= resistance - atr * 0.15 and close < resistance:
        return "PUT"

    return None


# =========================================================
# MAIN STRATEGY ENGINE
# =========================================================

def analyze_signal(candles_1m, candles_5m, symbol=None):

    if not valid_candles(candles_1m):
        return None

    if not valid_candles(candles_5m):
        return None

    if len(candles_1m) < 60 or len(candles_5m) < 250:
        return None

    # Do not mutate input lists.
    candles_1m = list(candles_1m)
    candles_5m = list(candles_5m)

    # Main trend on 15m and 5m.
    candles_15m = build_15m_candles(candles_5m)

    trend_15m = determine_trend(candles_15m)
    trend_5m = determine_trend(candles_5m)

    # Both timeframes must agree.
    if trend_15m not in ("CALL", "PUT"):
        return None

    if trend_5m != trend_15m:
        return None

    direction = trend_15m

    # Indicators.
    atr = calculate_atr(candles_1m, ATR_PERIOD)
    adx = calculate_adx(candles_5m, ADX_PERIOD)
    rsi = calculate_rsi(get_values(candles_1m, "close"), RSI_PERIOD)

    if atr is None or atr <= 0 or adx is None or rsi is None:
        return None

    # Avoid weak or extremely choppy trends.
    if adx < 18:
        return None

    # Independent strategy votes.
    strategies = {
        "EMA Trend": ema_signal(candles_5m),
        "Market Structure": market_structure(candles_5m),
        "Liquidity Sweep": liquidity_sweep(candles_1m),
        "False Breakout": liquidity_sweep(candles_5m),
        "Breakout Retest": breakout_retest(candles_1m),
        "Supply Demand": supply_demand(candles_5m),
        "Fair Value Gap": fair_value_gap(candles_1m),
        "Candle Pattern": candle_pattern(candles_1m),
        "Momentum RSI": momentum_signal(candles_1m),
        "MACD": macd_signal(candles_1m),
        "Divergence": divergence_signal(candles_1m),
        "Volatility Squeeze": volatility_squeeze(candles_1m),
        "Volume": volume_signal(candles_1m),
        "Pullback": pullback_signal(candles_1m),
        "Support Resistance": sr_rejection(candles_1m)
    }

    # Count only strategies agreeing with the main trend.
    matching = [
        name for name, result in strategies.items()
        if result == direction
    ]

    opposing = [
        name for name, result in strategies.items()
        if result in ("CALL", "PUT") and result != direction
    ]

    votes = len(matching)

    # Reject conflicting signals.
    if len(opposing) >= 3:
        return None

    if votes < MIN_VOTES:
        return None

    # Independent evidence groups.
    structure_group = any(
        strategies.get(name) == direction
        for name in (
            "Market Structure",
            "Liquidity Sweep",
            "False Breakout",
            "Breakout Retest"
        )
    )

    location_group = any(
        strategies.get(name) == direction
        for name in (
            "Support Resistance",
            "Supply Demand",
            "Fair Value Gap"
        )
    )

    confirmation_group = any(
        strategies.get(name) == direction
        for name in (
            "Candle Pattern",
            "Momentum RSI",
            "MACD",
            "Pullback"
        )
    )

    # Require evidence from different categories.
    groups_confirmed = sum([
        structure_group,
        location_group,
        confirmation_group
    ])

    if groups_confirmed < 2:
        return None

    # Final entry candle confirmation.
    quality_direction = candle_quality(candles_1m)

    if quality_direction != direction:
        return None

    current = candles_1m[-1]

    o = candle_value(current, "open")
    h = candle_value(current, "high")
    l = candle_value(current, "low")
    c = candle_value(current, "close")

    candle_range = h - l

    if candle_range <= 0:
        return None

    body_ratio = abs(c - o) / candle_range

    # Reject abnormally large entry candles.
    if candle_range > atr * 2.0:
        return None

    if candle_range < atr * 0.35:
        return None

    # Score.
    score = 0

    score += 3  # 15m + 5m trend agreement
    score += min(votes, 6)
    score += groups_confirmed
    score += 1 if adx >= 20 else 0
    score += 1 if body_ratio >= 0.60 else 0

    if score < MIN_SCORE:
        return None

    support, resistance = support_resistance(candles_5m, 40)

    reasons = matching[:]

    return {
        "direction": direction,
        "entry_reference_price": c,
        "pattern": "Multi-Strategy Confirmation",
        "score": score,
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "atr": atr,
        "candle_body_ratio": round(body_ratio, 2),
        "expiry_minutes": 1,
        "status": "DEMO_ONLY",
        "trend_15m": trend_15m,
        "trend_5m": trend_5m,
        "strategy_votes": votes,
        "confirmed_groups": groups_confirmed,
        "matching_strategies": reasons,
        "opposing_strategies": opposing,
        "support": support,
        "resistance": resistance
    }


# =========================================================
# DUPLICATE SIGNAL PROTECTION
# =========================================================

def is_new_signal(symbol, signal):

    if not signal:
        return False

    key = (
        symbol,
        signal.get("direction"),
        signal.get("pattern"),
        round(float(signal.get("entry_reference_price", 0)), 6)
    )

    if key in sent_signals:
        return False

    sent_signals.add(key)

    if len(sent_signals) > 3000:
        sent_signals.clear()
        sent_signals.add(key)

    return True
