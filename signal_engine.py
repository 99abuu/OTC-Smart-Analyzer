
import logging

logger = logging.getLogger(__name__)

MIN_CANDLES = 60
MIN_SCORE = 1

def value(candle, key, default=0):
    try:
        return float(candle.get(key, default))
    except (TypeError, ValueError, AttributeError):
        return float(default)

def valid_candles(candles):
    return (
        isinstance(candles, list)
        and len(candles) >= MIN_CANDLES
        and all(
            all(k in c for k in ("open", "high", "low", "close"))
            for c in candles
        )
    )

def ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for price in values[period:]:
        result = (price - result) * multiplier + result
    return result

def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "CALL"
    if candle["close"] < candle["open"]:
        return "PUT"
    return None

def add_signal(signals, name, direction, reason):
    if direction in ("CALL", "PUT"):
        signals.append({
            "strategy": name,
            "direction": direction,
            "reason": reason
        })

def analyze_signal(candles_1m, candles_5m, symbol=None):
    if not valid_candles(candles_1m):
        return None
    if not valid_candles(candles_5m):
        return None

    closes = [value(c, "close") for c in candles_1m]
    highs = [value(c, "high") for c in candles_1m]
    lows = [value(c, "low") for c in candles_1m]

    signals = []
    current = candles_1m[-1]
    previous = candles_1m[-2]
    price = closes[-1]

    # 1. EMA 50 / 200
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    if ema50 is not None and ema200 is not None:
        if price > ema50 > ema200:
            add_signal(signals, "EMA Trend", "CALL",
                       "السعر فوق EMA50 وEMA50 فوق EMA200")
        elif price < ema50 < ema200:
            add_signal(signals, "EMA Trend", "PUT",
                       "السعر تحت EMA50 وEMA50 تحت EMA200")

    # 2. RSI
    current_rsi = rsi(closes)
    if current_rsi is not None:
        if current_rsi <= 30:
            add_signal(signals, "RSI Oversold", "CALL",
                       f"RSI وصل إلى {current_rsi:.1f}")
        elif current_rsi >= 70:
            add_signal(signals, "RSI Overbought", "PUT",
                       f"RSI وصل إلى {current_rsi:.1f}")

    # 3. Support / Resistance
    support = min(lows[-21:-1])
    resistance = max(highs[-21:-1])
    candle_range = max(value(current, "high") - value(current, "low"), 1e-9)

    if value(current, "low") <= support + candle_range * 0.15:
        if current["close"] > current["open"]:
            add_signal(signals, "Support Rejection", "CALL",
                       "السعر ارتد صعوداً من منطقة الدعم")

    if value(current, "high") >= resistance - candle_range * 0.15:
        if current["close"] < current["open"]:
            add_signal(signals, "Resistance Rejection", "PUT",
                       "السعر ارتد هبوطاً من منطقة المقاومة")

    # 4. Breakout
    if current["close"] > resistance and previous["close"] <= resistance:
        add_signal(signals, "Resistance Breakout", "CALL",
                   "إغلاق فوق المقاومة السابقة")
    if current["close"] < support and previous["close"] >= support:
        add_signal(signals, "Support Breakdown", "PUT",
                   "إغلاق تحت الدعم السابق")

    # 5. Engulfing
    if (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    ):
        add_signal(signals, "Bullish Engulfing", "CALL",
                   "شمعة ابتلاعية صاعدة")

    if (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    ):
        add_signal(signals, "Bearish Engulfing", "PUT",
                   "شمعة ابتلاعية هابطة")

    # 6. Momentum
    if len(closes) >= 6:
        if closes[-1] > closes[-4] > closes[-6]:
            add_signal(signals, "Price Momentum", "CALL",
                       "زخم سعري صاعد خلال آخر الشموع")
        elif closes[-1] < closes[-4] < closes[-6]:
            add_signal(signals, "Price Momentum", "PUT",
                       "زخم سعري هابط خلال آخر الشموع")

    # 7. EMA 9 / 21 crossover
    ema9_now = ema(closes, 9)
    ema21_now = ema(closes, 21)
    ema9_prev = ema(closes[:-1], 9)
    ema21_prev = ema(closes[:-1], 21)
    if all(x is not None for x in (ema9_now, ema21_now, ema9_prev, ema21_prev)):
        if ema9_prev <= ema21_prev and ema9_now > ema21_now:
            add_signal(signals, "EMA 9/21 Cross", "CALL",
                       "تقاطع EMA9 فوق EMA21")
        elif ema9_prev >= ema21_prev and ema9_now < ema21_now:
            add_signal(signals, "EMA 9/21 Cross", "PUT",
                       "تقاطع EMA9 تحت EMA21")

    # 8. Pin bar
    body = abs(value(current, "close") - value(current, "open"))
    upper_wick = value(current, "high") - max(value(current, "open"), value(current, "close"))
    lower_wick = min(value(current, "open"), value(current, "close")) - value(current, "low")
    if lower_wick > body * 2 and lower_wick > upper_wick:
        add_signal(signals, "Bullish Pin Bar", "CALL",
                   "ذيل سفلي طويل يشير إلى رفض الهبوط")
    elif upper_wick > body * 2 and upper_wick > lower_wick:
        add_signal(signals, "Bearish Pin Bar", "PUT",
                   "ذيل علوي طويل يشير إلى رفض الصعود")

    # 9. Five-minute trend (independent signal)
    closes5 = [value(c, "close") for c in candles_5m]
    ema20_5 = ema(closes5, 20)
    ema50_5 = ema(closes5, 50)
    if ema20_5 is not None and ema50_5 is not None:
        if closes5[-1] > ema20_5 > ema50_5:
            add_signal(signals, "5m Trend", "CALL",
                       "الاتجاه على 5 دقائق صاعد")
        elif closes5[-1] < ema20_5 < ema50_5:
            add_signal(signals, "5m Trend", "PUT",
                       "الاتجاه على 5 دقائق هابط")

    # 10. MACD crossover
    if len(closes) >= 40:
        fast_now = ema(closes, 12)
        slow_now = ema(closes, 26)
        fast_prev = ema(closes[:-1], 12)
        slow_prev = ema(closes[:-1], 26)
        if all(x is not None for x in (fast_now, slow_now, fast_prev, slow_prev)):
            if fast_prev <= slow_prev and fast_now > slow_now:
                add_signal(signals, "MACD Direction", "CALL",
                           "تقاطع المتوسط السريع فوق البطيء")
            elif fast_prev >= slow_prev and fast_now < slow_now:
                add_signal(signals, "MACD Direction", "PUT",
                           "تقاطع المتوسط السريع تحت البطيء")

    if not signals:
        return None

    directions = {s["direction"] for s in signals}
    if len(directions) > 1:
        # لا نخفي التعارض: ننتظر حتى تتفق الإشارات الصادرة في هذه الشمعة
        return None

    direction = signals[0]["direction"]
    names = [s["strategy"] for s in signals]
    reasons = [s["reason"] for s in signals]

    return {
        "direction": direction,
        "entry_reference_price": price,
        "pattern": "Independent Strategy Signals",
        "score": len(signals),
        "rsi": current_rsi,
        "adx": 0,
        "atr": candle_range,
        "candle_body_ratio": body / candle_range,
        "expiry_minutes": 1,
        "status": "DEMO_ONLY",
        "strategy_votes": len(signals),
        "confirmed_groups": len(signals),
        "matching_strategies": names,
        "opposing_strategies": [],
        "strategy_reasons": reasons,
        "support": support,
        "resistance": resistance,
        "trend_15m": "NOT_USED",
        "trend_5m": "INDEPENDENT"
    }

_sent = set()

def is_new_signal(symbol, signal):
    if not signal:
        return False
    key = (
        symbol,
        signal["direction"],
        tuple(signal["matching_strategies"]),
        round(signal["entry_reference_price"], 6)
    )
    if key in _sent:
        return False
    _sent.add(key)
    if len(_sent) > 3000:
        _sent.clear()
    return True
