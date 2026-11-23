import asyncio
import logging
import threading
import queue
import time
import math

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from statistics import mean

from market_data import (
    fetch_otc_symbols,
    fetch_candles,
    stream_ticks
)

from signal_engine import (
    analyze_signal,
    is_new_signal,
    select_trade_duration
)

from trade_history import (
    record_signal,
    record_update,
    record_result,
    record_horizon_result
)

from ai_model import evaluate_signal
from ai_filter_model import evaluate_signal as evaluate_filter_signal

logger = logging.getLogger(__name__)

MAX_SYMBOLS = 5
MAX_CANDIDATES = 18
MARKET_REFRESH_SECONDS = 300

DEFAULT_TRADE_DURATION = 60
COMPARISON_DURATIONS = (60, 120)
MAX_HORIZON_DELAY = 10

PRE_ENTRY_SECONDS = 60
CONFIRM_BEFORE_ENTRY = 15
MAX_ENTRY_DELAY = 10

HISTORY_1M = 250
MIN_CANDLES_1M = 200
MAX_CANDLES = 400

OTC_TICK_TIME_OFFSET_SECONDS = 7200

MAX_CANDLE_START_DELAY = 2

LOCAL_TIMEZONE = ZoneInfo("Europe/Nicosia")

STOP_SETTLEMENT_TIMEOUT = 180

_candidate_rotation_offset = 0


# --------------------------------------------------
# GENERAL HELPERS
# --------------------------------------------------

def format_time(timestamp):
    local_time = datetime.fromtimestamp(
        timestamp,
        LOCAL_TIMEZONE
    ).strftime("%H:%M:%S")

    utc_time = datetime.fromtimestamp(
        timestamp,
        timezone.utc
    ).strftime("%H:%M:%S")

    return (
        f"{local_time} (توقيتك المحلي) "
        f"| {utc_time} UTC"
    )


def parse_number(value, default=0):
    try:
        if isinstance(value, str):
            value = value.replace("%", "").strip()

        number = float(value)

        if not math.isfinite(number):
            return default

        return number

    except (TypeError, ValueError):
        return default


def calculate_outcome(direction, entry_price, exit_price):
    if direction == "CALL":
        if exit_price > entry_price:
            return "WIN"
        if exit_price < entry_price:
            return "LOSS"
        return "DRAW"

    if direction == "PUT":
        if exit_price < entry_price:
            return "WIN"
        if exit_price > entry_price:
            return "LOSS"
        return "DRAW"

    return "DRAW"


async def send_status_message(send_status, message):
    if not send_status:
        logger.info("SCANNER STATUS: %s", message)
        return

    try:
        await send_status(message)
    except Exception:
        logger.exception("Status notification failed")


def safe_record_update(history_id, status, details=None):
    if not history_id:
        return False

    try:
        result = record_update(
            history_id,
            status,
            details or {}
        )

        return result is not False

    except Exception:
        logger.exception(
            "History update failed: %s | %s",
            history_id,
            status
        )
        return False


# --------------------------------------------------
# AI SHADOW EVALUATION - LOGISTIC REGRESSION
# --------------------------------------------------

def evaluate_ai_shadow(signal):
    """
    تقييم الإشارة باستخدام النموذج القديم في Shadow فقط.

    لا يغير اتجاه الإشارة أو مدتها.
    لا يمنع الإشارة.
    """

    try:
        result = evaluate_signal(signal)

        if not isinstance(result, dict):
            logger.error("AI returned an invalid result")

            return {
                "mode": "SHADOW",
                "error": "INVALID_AI_RESULT",
                "predictions": {}
            }

        predictions = result.get("predictions", {})

        if not isinstance(predictions, dict):
            predictions = {}

        for horizon in (60, 120):
            prediction = predictions.get(str(horizon), {})

            if not isinstance(prediction, dict):
                continue

            logger.info(
                "AI SHADOW: symbol=%s | horizon=%ss | "
                "status=%s | samples=%s | "
                "win_probability=%s",
                signal.get("symbol"),
                horizon,
                prediction.get("status"),
                prediction.get("samples"),
                prediction.get("win_probability")
            )

        return result

    except Exception:
        logger.exception(
            "AI shadow evaluation failed for %s",
            signal.get("symbol")
        )

        return {
            "mode": "SHADOW",
            "error": "EVALUATION_FAILED",
            "predictions": {}
        }


# --------------------------------------------------
# KNN SHADOW EVALUATION - NEW MODEL
# --------------------------------------------------

def evaluate_knn_shadow(signal):
    """
    تقييم مستقل باستخدام KNN في Shadow Mode.

    لا يغير:
    - اتجاه الإشارة.
    - الاستراتيجية.
    - مدة الإشارة.
    - قرار إرسال الإشارة.
    - قرار تأكيد الدخول.

    لا يطبق أي عتبة قبول أو رفض.
    """

    try:
        result = evaluate_filter_signal(signal)

        if not isinstance(result, dict):
            logger.error("KNN returned an invalid result")

            return {
                "mode": "SHADOW",
                "model": "KNN_Shadow_v1",
                "error": "INVALID_KNN_RESULT",
                "predictions": {}
            }

        predictions = result.get("predictions", {})

        if not isinstance(predictions, dict):
            predictions = {}

        for horizon in (60, 120):
            prediction = predictions.get(str(horizon), {})

            if not isinstance(prediction, dict):
                continue

            logger.info(
                "KNN SHADOW: symbol=%s | horizon=%ss | "
                "status=%s | samples=%s | probability=%s | "
                "validation=%s",
                signal.get("symbol"),
                horizon,
                prediction.get("status"),
                prediction.get("samples"),
                prediction.get("win_probability"),
                prediction.get("validation", {})
            )

        return result

    except Exception:
        logger.exception(
            "KNN shadow evaluation failed for %s",
            signal.get("symbol")
        )

        return {
            "mode": "SHADOW",
            "model": "KNN_Shadow_v1",
            "error": "EVALUATION_FAILED",
            "predictions": {}
        }


# --------------------------------------------------
# CANDLE VALIDATION AND TIME NORMALIZATION
# --------------------------------------------------

def valid_market_candles(candles):
    result = []

    if not isinstance(candles, list):
        return result

    for candle in candles:
        if not isinstance(candle, dict):
            continue

        try:
            o = parse_number(candle.get("open"), None)
            h = parse_number(candle.get("high"), None)
            l = parse_number(candle.get("low"), None)
            c = parse_number(candle.get("close"), None)

            if None in (o, h, l, c):
                continue

            if min(o, h, l, c) <= 0:
                continue

            if h < max(o, c, l):
                continue

            if l > min(o, c, h):
                continue

            normalized = {
                "open": o,
                "high": h,
                "low": l,
                "close": c
            }

            if "time" in candle:
                candle_time = parse_number(
                    candle.get("time"),
                    None
                )

                if candle_time is None or candle_time <= 0:
                    continue

                normalized["time"] = int(candle_time)

            result.append(normalized)

        except (TypeError, ValueError, OverflowError):
            continue

    return result


def normalize_api_candles(raw_candles):
    valid = []

    if not isinstance(raw_candles, list):
        return valid

    for candle in raw_candles:
        if not isinstance(candle, dict):
            continue

        raw_time = parse_number(
            candle.get("time"),
            None
        )

        if raw_time is None or raw_time <= 0:
            continue

        normalized_time = int(
            raw_time - OTC_TICK_TIME_OFFSET_SECONDS
        )

        normalized = dict(candle)
        normalized["time"] = normalized_time

        valid.append(normalized)

    valid = valid_market_candles(valid)
    valid.sort(key=lambda item: item["time"])

    unique = {}

    for candle in valid:
        unique[candle["time"]] = candle

    return [
        unique[timestamp]
        for timestamp in sorted(unique)
    ]


def has_recent_contiguous_candles(
    candles,
    minimum=MIN_CANDLES_1M
):
    if len(candles) < minimum:
        return False

    recent = candles[-minimum:]

    for index in range(1, len(recent)):
        previous_time = recent[index - 1].get("time")
        current_time = recent[index].get("time")

        if previous_time is None or current_time is None:
            return False

        if current_time - previous_time != 60:
            return False

    return True


def get_last_candle_time(candles):
    if not candles:
        return None

    return candles[-1].get("time")


def add_candle(candles, candle):
    candle_time = candle.get("time")

    if candle_time is not None:
        for existing in reversed(candles[-5:]):
            if existing.get("time") == candle_time:
                return

        last_time = get_last_candle_time(candles)

        if last_time is not None and candle_time < last_time:
            return

    candles.append(dict(candle))

    if len(candles) > MAX_CANDLES:
        del candles[:-MAX_CANDLES]


# --------------------------------------------------
# MARKET SCORING
# --------------------------------------------------

def market_score(candles, payout):
    candles = valid_market_candles(candles)

    if len(candles) < MIN_CANDLES_1M:
        return 0

    recent = candles[-60:]

    ranges = [
        (c["high"] - c["low"]) / c["close"] * 100
        for c in recent
        if c["close"] > 0
    ]

    if not ranges:
        return 0

    average_range = mean(ranges)

    volatility_score = max(
        0,
        100 - abs(average_range - 0.12) / 0.20 * 100
    )

    closes = [c["close"] for c in recent]

    movements = [
        abs(closes[i] - closes[i - 1])
        for i in range(1, len(closes))
    ]

    total_movement = sum(movements)

    if total_movement > 0:
        consistency = (
            abs(closes[-1] - closes[0]) / total_movement
        ) * 100
    else:
        consistency = 0

    consistency = max(0, min(100, consistency))

    bullish = sum(
        1 for c in recent
        if c["close"] > c["open"]
    )

    bearish = sum(
        1 for c in recent
        if c["close"] < c["open"]
    )

    directional_candles = bullish + bearish

    if directional_candles:
        trend_score = (
            max(bullish, bearish) / directional_candles
        ) * 100
    else:
        trend_score = 0

    average_atr = mean([
        c["high"] - c["low"]
        for c in recent
    ])

    tolerance = max(
        average_atr * 0.30,
        closes[-1] * 0.0001
    )

    pivot_highs = []
    pivot_lows = []

    for i in range(2, len(recent) - 2):
        high = recent[i]["high"]
        low = recent[i]["low"]

        if all(
            high >= recent[j]["high"]
            for j in range(i - 2, i + 3)
            if j != i
        ):
            pivot_highs.append(high)

        if all(
            low <= recent[j]["low"]
            for j in range(i - 2, i + 3)
            if j != i
        ):
            pivot_lows.append(low)

    def count_level_groups(levels):
        groups = []

        for level in levels:
            matched = False

            for group in groups:
                if abs(level - group[0]) <= tolerance:
                    group.append(level)
                    matched = True
                    break

            if not matched:
                groups.append([level])

        return sum(
            1 for group in groups
            if len(group) >= 2
        )

    level_groups = (
        count_level_groups(pivot_highs)
        + count_level_groups(pivot_lows)
    )

    support_resistance_score = min(
        100,
        level_groups * 20
    )

    data_score = min(
        100,
        len(candles) / HISTORY_1M * 100
    )

    payout_score = max(
        0,
        min(100, payout)
    )

    final_score = (
        volatility_score * 0.25
        + consistency * 0.20
        + support_resistance_score * 0.15
        + data_score * 0.20
        + trend_score * 0.10
        + payout_score * 0.10
    )

    return round(max(0, min(100, final_score)), 2)


# --------------------------------------------------
# ROTATING MARKET CANDIDATE SELECTION
# --------------------------------------------------

def select_market_candidates(open_symbols, current_symbols=None):
    global _candidate_rotation_offset

    if not open_symbols:
        return []

    ordered = sorted(
        open_symbols,
        key=lambda item: (
            -parse_number(item.get("payout")),
            str(item.get("symbol", ""))
        )
    )

    total_markets = len(ordered)

    target_count = min(
        MAX_CANDIDATES,
        total_markets
    )

    if target_count == 0:
        return []

    rotation_offset = (
        _candidate_rotation_offset % total_markets
    )

    rotated = (
        ordered[rotation_offset:]
        + ordered[:rotation_offset]
    )

    if target_count == 1:
        indexes = [0]
    else:
        indexes = [
            round(i * total_markets / target_count)
            for i in range(target_count)
        ]

    selected = []
    seen = set()

    for index in indexes:
        item = rotated[index % total_markets]
        symbol = item.get("symbol")

        if not symbol or symbol in seen:
            continue

        selected.append(item)
        seen.add(symbol)

    if len(selected) < target_count:
        for item in rotated:
            symbol = item.get("symbol")

            if not symbol or symbol in seen:
                continue

            selected.append(item)
            seen.add(symbol)

            if len(selected) >= target_count:
                break

    _candidate_rotation_offset = (
        rotation_offset + 1
    ) % total_markets

    logger.info(
        "CANDIDATE ROTATION: offset=%s | "
        "open_markets=%s | candidates=%s | symbols=%s",
        rotation_offset,
        total_markets,
        len(selected),
        [item["symbol"] for item in selected]
    )

    return selected


# --------------------------------------------------
# MARKET DATA HELPERS
# --------------------------------------------------

def get_open_otc_symbols():
    symbols_data = fetch_otc_symbols()

    if not symbols_data:
        return []

    return [
        item for item in symbols_data
        if isinstance(item, dict)
        and item.get("isOtc") is True
        and item.get("open") is True
        and item.get("symbol")
    ]


def rank_otc_markets(current_symbols=None):
    current_symbols = current_symbols or []

    open_symbols = get_open_otc_symbols()

    if not open_symbols:
        return [], {}

    candidates = select_market_candidates(open_symbols)

    candidate_symbols = [
        item["symbol"]
        for item in candidates
    ]

    ranked = []
    histories = {}

    current_bucket = int(time.time() // 60) * 60

    logger.info(
        "Market candidate evaluation: %s candidates from %s open OTC markets",
        len(candidates),
        len(open_symbols)
    )

    logger.info(
        "CANDIDATE MARKETS TO EVALUATE: %s",
        candidate_symbols
    )

    for item in candidates:
        symbol = item["symbol"]

        try:
            raw_candles = fetch_candles(
                symbol,
                60,
                HISTORY_1M
            )

            candles = normalize_api_candles(raw_candles)

            candles = [
                candle for candle in candles
                if candle["time"] < current_bucket
            ]

            if not has_recent_contiguous_candles(
                candles,
                MIN_CANDLES_1M
            ):
                logger.warning(
                    "Market skipped: %s | "
                    "insufficient or discontinuous candle history",
                    symbol
                )
                continue

            payout = parse_number(item.get("payout"))

            score = market_score(candles, payout)

            if score <= 0:
                continue

            ranked.append({
                "symbol": symbol,
                "score": score,
                "payout": payout
            })

            histories[symbol] = candles[-MAX_CANDLES:]

        except Exception:
            logger.exception(
                "Market evaluation failed: %s",
                symbol
            )

    ranked.sort(
        key=lambda item: (
            -item["score"],
            item["symbol"]
        )
    )

    logger.info(
        "MARKET EVALUATION RESULTS: %s",
        [
            {
                "symbol": item["symbol"],
                "score": item["score"],
                "payout": item["payout"]
            }
            for item in ranked
        ]
    )

    selected = ranked[:MAX_SYMBOLS]

    old_symbols = set(current_symbols)
    new_symbols = {
        item["symbol"]
        for item in selected
    }

    logger.info(
        "MARKET SELECTION CHANGES: entered=%s | exited=%s",
        sorted(new_symbols - old_symbols),
        sorted(old_symbols - new_symbols)
    )

    logger.info(
        "FINAL TOP %s MARKETS: %s",
        MAX_SYMBOLS,
        [
            {
                "symbol": item["symbol"],
                "score": item["score"],
                "payout": item["payout"]
            }
            for item in selected
        ]
    )

    logger.info(
        "Market ranking completed: evaluated=%s | eligible=%s",
        len(candidates),
        len(ranked)
    )

    return selected, histories


def load_manual_markets(selected_symbols):
    """
    Load only the markets explicitly selected by the user.
    Invalid or unavailable selections are not automatically replaced.
    """

    if not isinstance(selected_symbols, (list, tuple)):
        return [], {}, list(selected_symbols or [])

    selected_symbols = list(dict.fromkeys(
        str(symbol).strip()
        for symbol in selected_symbols
        if symbol
    ))

    if not selected_symbols:
        return [], {}, []

    if len(selected_symbols) > MAX_SYMBOLS:
        raise ValueError(
            f"الحد الأقصى لاختيار الأسواق هو {MAX_SYMBOLS}"
        )

    open_symbols = get_open_otc_symbols()

    open_map = {
        item["symbol"]: item
        for item in open_symbols
    }

    selected = []
    histories = {}
    unavailable = []

    current_bucket = int(time.time() // 60) * 60

    for symbol in selected_symbols:
        item = open_map.get(symbol)

        if not item:
            unavailable.append(symbol)
            logger.warning(
                "MANUAL MARKET UNAVAILABLE: %s",
                symbol
            )
            continue

        try:
            raw_candles = fetch_candles(
                symbol,
                60,
                HISTORY_1M
            )

            candles = normalize_api_candles(raw_candles)

            candles = [
                candle for candle in candles
                if candle["time"] < current_bucket
            ]

            if not has_recent_contiguous_candles(
                candles,
                MIN_CANDLES_1M
            ):
                unavailable.append(symbol)

                logger.warning(
                    "MANUAL MARKET SKIPPED: %s | "
                    "insufficient or discontinuous candle history",
                    symbol
                )
                continue

            payout = parse_number(item.get("payout"))
            score = market_score(candles, payout)

            selected.append({
                "symbol": symbol,
                "score": score,
                "payout": payout
            })

            histories[symbol] = candles[-MAX_CANDLES:]

            logger.info(
                "MANUAL MARKET LOADED: %s | score=%s | payout=%s",
                symbol,
                score,
                payout
            )

        except Exception:
            unavailable.append(symbol)
            logger.exception(
                "MANUAL MARKET LOAD FAILED: %s",
                symbol
            )

    logger.info(
        "MANUAL MARKET SELECTION: selected=%s | unavailable=%s",
        [item["symbol"] for item in selected],
        unavailable
    )

    return selected, histories, unavailable


# --------------------------------------------------
# HISTORY RESYNCHRONIZATION
# --------------------------------------------------

def fetch_resynchronized_history(symbol, current_bucket):
    try:
        raw_candles = fetch_candles(
            symbol,
            60,
            HISTORY_1M
        )

        candles = normalize_api_candles(raw_candles)

        candles = [
            candle for candle in candles
            if candle["time"] < current_bucket
        ]

        if not candles:
            logger.warning(
                "RESYNC FAILED: %s | API returned no completed candles",
                symbol
            )
            return None

        expected_last_time = current_bucket - 60
        actual_last_time = get_last_candle_time(candles)

        if actual_last_time != expected_last_time:
            logger.warning(
                "RESYNC FAILED: %s | "
                "expected latest=%s | actual latest=%s",
                symbol,
                expected_last_time,
                actual_last_time
            )
            return None

        if not has_recent_contiguous_candles(
            candles,
            MIN_CANDLES_1M
        ):
            logger.warning(
                "RESYNC FAILED: %s | "
                "recent candles are not continuous",
                symbol
            )
            return None

        logger.info(
            "RESYNC SUCCESS: %s | candles=%s | latest=%s",
            symbol,
            len(candles),
            actual_last_time
        )

        return candles[-MAX_CANDLES:]

    except Exception:
        logger.exception("RESYNC ERROR: %s", symbol)
        return None


# --------------------------------------------------
# LIVE STREAM
# --------------------------------------------------

def stream_worker(symbols, tick_queue, stop_event):
    while not stop_event.is_set():
        try:
            logger.info(
                "Connecting to OTC live stream: %s",
                symbols
            )

            for event in stream_ticks(symbols):
                if stop_event.is_set():
                    break

                if not isinstance(event, dict):
                    continue

                if event.get("_event") == "tick":
                    tick_queue.put(event)

                elif event.get("_event") == "closed":
                    logger.warning("OTC stream closed")
                    break

        except Exception:
            logger.exception("OTC stream error")

        if not stop_event.is_set():
            time.sleep(5)


# --------------------------------------------------
# SIGNAL VALIDATION - COMPLETED CANDLES ONLY
# --------------------------------------------------

def validate_pending_signal(
    alert,
    price,
    candles_1m,
    current_candle,
    timestamp
):
    if len(candles_1m) < MIN_CANDLES_1M:
        return False, "بيانات الدقيقة غير كافية"

    if not has_recent_contiguous_candles(
        candles_1m,
        MIN_CANDLES_1M
    ):
        return False, "تسلسل بيانات الدقيقة غير مكتمل"

    if not isinstance(current_candle, dict):
        return False, "الشمعة الحية غير متوفرة"

    live_bucket = parse_number(
        current_candle.get(
            "bucket",
            current_candle.get("time")
        ),
        None
    )

    if live_bucket is None or live_bucket <= 0:
        return False, "توقيت الشمعة الحية غير صالح"

    live_bucket = int(live_bucket)

    expected_live_bucket = int(timestamp // 60) * 60

    if live_bucket != expected_live_bucket:
        return False, "الشمعة الحية لا تطابق توقيت السوق الحالي"

    last_history_time = get_last_candle_time(candles_1m)

    if last_history_time is None:
        return False, "توقيت آخر شمعة مكتملة غير متوفر"

    if live_bucket != last_history_time + 60:
        return False, "الشمعة الحية غير متصلة بالبيانات السابقة"

    live_snapshot = {
        "time": live_bucket,
        "open": current_candle.get("open"),
        "high": current_candle.get("high"),
        "low": current_candle.get("low"),
        "close": current_candle.get("close")
    }

    validated_live = valid_market_candles([live_snapshot])

    if len(validated_live) != 1:
        return False, "بيانات الشمعة الحية غير صالحة"

    original = alert["signal"]
    direction = original.get("direction")

    fresh_signal = analyze_signal(
        candles_1m,
        symbol=original.get("symbol")
    )

    if not fresh_signal:
        return False, "لم تعد توجد إشارة مؤكدة على الشموع المكتملة"

    if fresh_signal.get("direction") != direction:
        return False, "تغير اتجاه الإشارة"

    original_strategies = set(
        original.get("matching_strategies", [])
    )

    fresh_strategies = set(
        fresh_signal.get("matching_strategies", [])
    )

    if original_strategies and not (
        original_strategies & fresh_strategies
    ):
        return False, "الاستراتيجية لم تعد تؤكد الإشارة"

    original_price = parse_number(
        original.get("price"),
        None
    )

    if original_price is None or original_price <= 0:
        return False, "سعر الإشارة غير صالح"

    live_price = parse_number(price, None)

    if live_price is None or live_price <= 0:
        return False, "السعر الحالي غير صالح"

    movement = abs(
        live_price - original_price
    ) / original_price

    if movement > 0.002:
        return False, "تحرك السعر بعيداً عن نقطة الإشارة"

    return True, "تم تأكيد الإشارة مع ثبات الاستراتيجية والسعر"
    # --------------------------------------------------
# MAIN SCANNER
# --------------------------------------------------

async def scanner_loop(
    send_signal,
    send_result,
    selection_queue=None,
    send_status=None
):
    logger.info("OTC Smart Analyzer starting")
    logger.info("Analysis timeframe: 1m only")
    logger.info("Manual market selection enabled")
    logger.info("Trade history recording enabled")
    logger.info("AI Shadow Mode enabled")
    logger.info("KNN Shadow Mode enabled")

    logger.info(
        "Dual-duration comparison enabled: %s seconds",
        COMPARISON_DURATIONS
    )

    logger.info("Dynamic duration selection enabled")

    logger.info(
        "Market refresh interval: %s seconds",
        MARKET_REFRESH_SECONDS
    )

    logger.info(
        "1m candle history target: %s",
        HISTORY_1M
    )

    logger.info(
        "OTC candle and tick timestamp correction: -%s seconds",
        OTC_TICK_TIME_OFFSET_SECONDS
    )

    logger.info(
        "Maximum live candle start delay: %s seconds",
        MAX_CANDLE_START_DELAY
    )

    logger.info("Display timezone: Europe/Nicosia")

    if selection_queue is None:
        logger.error(
            "Scanner requires a selection_queue. "
            "Waiting for Telegram market selection is mandatory."
        )

        await send_status_message(
            send_status,
            "تعذر تشغيل الماسح: قناة اختيار الأسواق غير مهيأة."
        )
        return

    # ----------------------------------------------
    # WAIT FOR INITIAL USER SELECTION
    # ----------------------------------------------

    await send_status_message(
        send_status,
        "الماسح جاهز. اختر الأسواق من Telegram ثم اضغط تأكيد."
    )

    logger.info("Waiting for initial market selection")

    initial_selection = None
    selection_mode = None
    initial_histories = {}

    while initial_selection is None:
        try:
            command = await asyncio.wait_for(
                selection_queue.get(),
                timeout=1
            )

        except asyncio.TimeoutError:
            continue

        if not isinstance(command, dict):
            continue

        mode = command.get("mode")

        if mode == "stop":
            logger.info(
                "STOP received before scanner startup"
            )
            await send_status_message(
                send_status,
                "الماسح متوقف. اضغط START لاختيار الأسواق."
            )
            return

        if mode == "manual":
            requested = command.get("symbols", [])

            try:
                selected, histories, unavailable = (
                    await asyncio.to_thread(
                        load_manual_markets,
                        requested
                    )
                )

            except Exception:
                logger.exception(
                    "Initial manual market loading failed"
                )
                continue

            if unavailable:
                await send_status_message(
                    send_status,
                    "تعذر تحميل بعض الأسواق: "
                    + ", ".join(unavailable)
                )

            if not selected:
                await send_status_message(
                    send_status,
                    "لم أتمكن من تحميل أي سوق صالح. "
                    "اختر أسواقاً أخرى."
                )
                continue

            initial_selection = selected
            initial_histories = histories
            selection_mode = "manual"

        elif mode == "auto":
            selected, histories = await asyncio.to_thread(
                rank_otc_markets,
                []
            )

            if not selected:
                await send_status_message(
                    send_status,
                    "لم يتم العثور على أسواق تلقائية صالحة. "
                    "اختر الأسواق يدوياً أو حاول لاحقاً."
                )
                continue

            initial_selection = selected
            initial_histories = histories
            selection_mode = "auto"

    selected = initial_selection

    symbols = [
        item["symbol"]
        for item in selected
    ]

    logger.info(
        "Initial market mode: %s",
        selection_mode
    )

    logger.info(
        "Selected OTC markets: %s",
        [
            (item["symbol"], item["score"])
            for item in selected
        ]
    )

    await send_status_message(
        send_status,
        "بدأت مراقبة الأسواق: " + ", ".join(symbols)
    )

    candles_1m = {
        symbol: initial_histories.get(symbol, [])
        for symbol in symbols
    }

    current_1m = {}
    current_1m_complete = {}

    data_ready = {
        symbol: has_recent_contiguous_candles(
            candles_1m[symbol],
            MIN_CANDLES_1M
        )
        for symbol in symbols
    }

    for symbol in symbols:
        logger.info(
            "History loaded: %s | 1m=%s | ready=%s",
            symbol,
            len(candles_1m[symbol]),
            data_ready[symbol]
        )

    tick_queue = queue.Queue()
    stop_event = threading.Event()

    worker = threading.Thread(
        target=stream_worker,
        args=(symbols, tick_queue, stop_event),
        daemon=True
    )

    worker.start()

    pending_alerts = {}
    pending_trades = {}
    last_signal_time = {}

    pending_selection = None
    stop_requested = False
    stop_requested_at = None

    next_refresh = (
        time.monotonic() + MARKET_REFRESH_SECONDS
        if selection_mode == "auto"
        else float("inf")
    )

    # ----------------------------------------------
    # CANCEL AN UNENTERED ALERT
    # ----------------------------------------------

    async def cancel_alert(symbol, alert, reason):
        message = dict(alert["signal"])
        message["status"] = "CANCELLED"
        message["reason"] = reason

        safe_record_update(
            alert.get("history_id"),
            "CANCELLED",
            {"reason": reason}
        )

        try:
            await send_signal(message)

        except Exception:
            logger.exception(
                "Cancellation message failed for %s",
                symbol
            )

        logger.info(
            "SIGNAL CANCELLED: %s | %s",
            symbol,
            reason
        )

        pending_alerts.pop(symbol, None)

    # ----------------------------------------------
    # APPLY A MARKET SELECTION
    # ----------------------------------------------

    async def apply_selection(
        new_selection,
        new_histories,
        new_mode
    ):
        nonlocal symbols
        nonlocal tick_queue
        nonlocal stop_event
        nonlocal worker
        nonlocal candles_1m
        nonlocal current_1m
        nonlocal current_1m_complete
        nonlocal data_ready
        nonlocal selection_mode
        nonlocal next_refresh

        new_symbols = [
            item["symbol"]
            for item in new_selection
        ]

        if not new_symbols:
            return False

        if pending_alerts or pending_trades:
            return False

        if new_symbols == symbols:
            selection_mode = new_mode

            next_refresh = (
                time.monotonic() + MARKET_REFRESH_SECONDS
                if new_mode == "auto"
                else float("inf")
            )

            logger.info(
                "Market list unchanged; mode updated to %s",
                new_mode
            )
            return True

        logger.info(
            "Changing OTC markets: %s -> %s",
            symbols,
            new_symbols
        )

        stop_event.set()

        await asyncio.to_thread(
            worker.join,
            8
        )

        if worker.is_alive():
            logger.warning(
                "Old stream has not stopped yet; "
                "market selection postponed"
            )

            # لا نمسح stop_event هنا.
            # يجب أن يتوقف العامل القديم قبل إنشاء عامل جديد.
            return False

        symbols = new_symbols
        selection_mode = new_mode

        candles_1m = {
            symbol: new_histories.get(symbol, [])
            for symbol in symbols
        }

        data_ready = {
            symbol: has_recent_contiguous_candles(
                candles_1m[symbol],
                MIN_CANDLES_1M
            )
            for symbol in symbols
        }

        current_1m = {}
        current_1m_complete = {}

        tick_queue = queue.Queue()
        stop_event = threading.Event()

        worker = threading.Thread(
            target=stream_worker,
            args=(symbols, tick_queue, stop_event),
            daemon=True
        )

        worker.start()

        next_refresh = (
            time.monotonic() + MARKET_REFRESH_SECONDS
            if new_mode == "auto"
            else float("inf")
        )

        logger.info(
            "New OTC markets active: %s | mode=%s",
            [
                (item["symbol"], item["score"])
                for item in new_selection
            ],
            selection_mode
        )

        await send_status_message(
            send_status,
            "تم تحديث الأسواق: " + ", ".join(symbols)
        )

        return True

    # ----------------------------------------------
    # CONTROL QUEUE
    # ----------------------------------------------

    async def process_control_commands():
        nonlocal pending_selection
        nonlocal stop_requested
        nonlocal stop_requested_at

        while True:
            try:
                command = selection_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if not isinstance(command, dict):
                continue

            mode = command.get("mode")

            if mode == "stop":
                if not stop_requested:
                    stop_requested = True
                    stop_requested_at = time.monotonic()

                    logger.info(
                        "STOP requested: preventing new signals"
                    )

                    await send_status_message(
                        send_status,
                        "تم طلب الإيقاف. لن تصدر إشارات جديدة، "
                        "وسأنتظر انتهاء الصفقات التجريبية المفتوحة."
                    )

                    for symbol, alert in list(
                        pending_alerts.items()
                    ):
                        await cancel_alert(
                            symbol,
                            alert,
                            "إيقاف الماسح بطلب المستخدم قبل الدخول"
                        )

                    pending_selection = None

                continue

            if stop_requested:
                logger.info(
                    "Ignoring selection command while stopping"
                )
                continue

            if mode == "manual":
                requested = command.get("symbols", [])

                if not requested:
                    await send_status_message(
                        send_status,
                        "لم يتم إرسال أي سوق للاختيار."
                    )
                    continue

                try:
                    new_selection, new_histories, unavailable = (
                        await asyncio.to_thread(
                            load_manual_markets,
                            requested
                        )
                    )

                except Exception:
                    logger.exception(
                        "Manual market selection failed"
                    )
                    continue

                if unavailable:
                    await send_status_message(
                        send_status,
                        "تعذر تحميل بعض الأسواق: "
                        + ", ".join(unavailable)
                    )

                if not new_selection:
                    await send_status_message(
                        send_status,
                        "لم يتم تطبيق الاختيار؛ لا توجد أسواق صالحة."
                    )
                    continue

                if pending_alerts or pending_trades:
                    pending_selection = (
                        new_selection,
                        new_histories,
                        "manual"
                    )

                    logger.info(
                        "Manual market change postponed "
                        "until pending alerts/trades finish"
                    )

                    await send_status_message(
                        send_status,
                        "تم استلام اختيار الأسواق. "
                        "سيتم تطبيقه بعد انتهاء الإشارات والصفقات الحالية."
                    )

                else:
                    applied = await apply_selection(
                        new_selection,
                        new_histories,
                        "manual"
                    )

                    if not applied:
                        pending_selection = (
                            new_selection,
                            new_histories,
                            "manual"
                        )

                continue

            if mode == "auto":
                try:
                    new_selection, new_histories = (
                        await asyncio.to_thread(
                            rank_otc_markets,
                            symbols
                        )
                    )

                except Exception:
                    logger.exception(
                        "Automatic market selection failed"
                    )
                    continue

                if not new_selection:
                    await send_status_message(
                        send_status,
                        "لم يتم العثور على أسواق تلقائية صالحة."
                    )
                    continue

                if pending_alerts or pending_trades:
                    pending_selection = (
                        new_selection,
                        new_histories,
                        "auto"
                    )

                    await send_status_message(
                        send_status,
                        "سيتم تفعيل الاختيار التلقائي "
                        "بعد انتهاء الصفقات الحالية."
                    )

                else:
                    applied = await apply_selection(
                        new_selection,
                        new_histories,
                        "auto"
                    )

                    if not applied:
                        pending_selection = (
                            new_selection,
                            new_histories,
                            "auto"
                        )

    # ----------------------------------------------
    # PAPER TRADE RESULT PROCESSING
    # ----------------------------------------------

    async def process_trade_results(symbol, trade, timestamp, price):
        """
        حفظ نتائج المقارنة لكل مدة بشكل مستقل.
        إذا فات توقيت المدة المختارة، تسجل UNRESOLVED
        دون اختلاق نتيجة WIN أو LOSS.
        """

        for horizon in COMPARISON_DURATIONS:

            if horizon in trade["processed_horizons"]:
                continue

            target_time = trade["entry_time"] + horizon

            if timestamp < target_time:
                continue

            delay = timestamp - target_time

            # --------------------------------------
            # HORIZON RESULT MISSED
            # --------------------------------------

            if delay > MAX_HORIZON_DELAY:

                missed_saved = safe_record_update(
                    trade.get("history_id"),
                    "HORIZON_RESULT_MISSED",
                    {
                        "horizon_seconds": horizon,
                        "target_time": target_time,
                        "observed_time": timestamp,
                        "delay_seconds": delay
                    }
                )

                if not missed_saved:
                    logger.error(
                        "HORIZON MISSED STATUS SAVE FAILED: %s | %ss",
                        symbol,
                        horizon
                    )
                    continue

                # إذا كانت هذه هي المدة المختارة،
                # نسجل النتيجة الأساسية كغير محسومة.
                if horizon == trade["selected_duration_seconds"]:

                    unresolved_saved = safe_record_update(
                        trade.get("history_id"),
                        "PRIMARY_RESULT_UNRESOLVED",
                        {
                            "symbol": symbol,
                            "horizon_seconds": horizon,
                            "target_time": target_time,
                            "observed_time": timestamp,
                            "delay_seconds": delay,
                            "reason": "MAX_HORIZON_DELAY_EXCEEDED",
                            "result": "UNRESOLVED"
                        }
                    )

                    if not unresolved_saved:
                        logger.error(
                            "PRIMARY UNRESOLVED SAVE FAILED: %s | %ss",
                            symbol,
                            horizon
                        )
                        continue

                    trade["primary_result_unresolved"] = True

                    logger.warning(
                        "PRIMARY RESULT UNRESOLVED: %s | %ss",
                        symbol,
                        horizon
                    )

                trade["processed_horizons"].add(horizon)

                logger.warning(
                    "HORIZON RESULT MISSED: %s | %ss | delay=%ss",
                    symbol,
                    horizon,
                    delay
                )

                continue

            # --------------------------------------
            # CALCULATE AND SAVE HORIZON RESULT
            # --------------------------------------

            outcome = calculate_outcome(
                trade["direction"],
                trade["entry_price"],
                price
            )

            horizon_details = {
                "symbol": symbol,
                "direction": trade["direction"],
                "strategies": trade.get("strategies", []),
                "strategy_reasons": trade.get(
                    "strategy_reasons",
                    []
                ),
                "entry_time": trade["entry_time"],
                "target_time": target_time,
                "observed_time": timestamp,
                "delay_seconds": delay,
                "measurement_price": price,
                "selected_duration_seconds": trade[
                    "selected_duration_seconds"
                ]
            }

            try:
                saved = record_horizon_result(
                    trade.get("history_id"),
                    horizon,
                    outcome,
                    entry_price=trade["entry_price"],
                    exit_price=price,
                    details=horizon_details
                )

            except Exception:
                logger.exception(
                    "HORIZON RESULT SAVE ERROR: %s | %ss",
                    symbol,
                    horizon
                )
                saved = False

            if not saved:
                logger.error(
                    "HORIZON RESULT SAVE FAILED: %s | %ss",
                    symbol,
                    horizon
                )
                continue

            trade["processed_horizons"].add(horizon)

            logger.info(
                "HORIZON RESULT: %s | %ss | %s | "
                "entry=%s | exit=%s | delay=%ss",
                symbol,
                horizon,
                outcome,
                trade["entry_price"],
                price,
                delay
            )

            # --------------------------------------
            # PREPARE PRIMARY RESULT
            # --------------------------------------

            if horizon == trade["selected_duration_seconds"]:

                trade["primary_result_data"] = {
                    "symbol": symbol,
                    "direction": trade["direction"],
                    "entry_price": trade["entry_price"],
                    "exit_price": price,
                    "entry_time": format_time(
                        trade["entry_time"]
                    ),
                    "exit_time": format_time(timestamp),
                    "result": outcome,
                    "duration_seconds": horizon,
                    "duration_reason": trade.get(
                        "duration_reason",
                        ""
                    ),
                    "strategies": trade.get("strategies", []),
                    "strategy_reasons": trade.get(
                        "strategy_reasons",
                        []
                    )
                }

                trade["primary_result_details"] = horizon_details

        # ------------------------------------------
        # RETRY PRIMARY RESULT SAVE
        # ------------------------------------------

        if (
            trade.get("primary_result_data")
            and not trade.get("primary_result_saved")
        ):
            primary = trade["primary_result_data"]

            try:
                primary_saved = record_result(
                    trade.get("history_id"),
                    primary["result"],
                    entry_price=primary["entry_price"],
                    exit_price=primary["exit_price"],
                    details=trade.get(
                        "primary_result_details",
                        {}
                    )
                )

            except Exception:
                logger.exception(
                    "PRIMARY RESULT SAVE ERROR: %s",
                    symbol
                )
                primary_saved = False

            if primary_saved:
                trade["primary_result_saved"] = True

                logger.info(
                    "PRIMARY RESULT SAVED: %s | %s | %s",
                    symbol,
                    primary["duration_seconds"],
                    primary["result"]
                )

            else:
                logger.error(
                    "PRIMARY RESULT SAVE FAILED; WILL RETRY: %s",
                    symbol
                )

        # ------------------------------------------
        # DELIVER PRIMARY RESULT AFTER SAVING
        # ------------------------------------------

        if (
            trade.get("primary_result_saved")
            and not trade.get("primary_result_delivered")
        ):
            primary = trade.get("primary_result_data")

            if primary:
                try:
                    delivery_response = await send_result(primary)
                    delivered = delivery_response is not False

                except Exception:
                    logger.exception(
                        "Selected-duration result delivery failed: %s",
                        symbol
                    )
                    delivered = False

                if delivered:
                    trade["primary_result_delivered"] = True

                    safe_record_update(
                        trade.get("history_id"),
                        "PRIMARY_RESULT_DELIVERED",
                        {
                            "duration_seconds": primary[
                                "duration_seconds"
                            ],
                            "result": primary["result"]
                        }
                    )

                    logger.info(
                        "PAPER RESULT DELIVERED: %s | %s | %s",
                        symbol,
                        primary["duration_seconds"],
                        primary["result"]
                    )

                else:
                    logger.warning(
                        "PRIMARY RESULT NOT DELIVERED; "
                        "WILL RETRY: %s",
                        symbol
                    )

        # ------------------------------------------
        # COMPLETION CHECK
        # ------------------------------------------

        horizons_completed = all(
            horizon in trade["processed_horizons"]
            for horizon in COMPARISON_DURATIONS
        )

        primary_horizon = trade["selected_duration_seconds"]

        primary_horizon_completed = (
            primary_horizon in trade["processed_horizons"]
        )

        primary_resolved_or_unresolved = (
            trade.get("primary_result_saved", False)
            or trade.get("primary_result_unresolved", False)
        )

        if (
            horizons_completed
            and primary_horizon_completed
            and primary_resolved_or_unresolved
        ):
            if not trade.get("comparison_completed"):

                comparison_saved = safe_record_update(
                    trade.get("history_id"),
                    "DURATION_COMPARISON_COMPLETED",
                    {
                        "durations": list(COMPARISON_DURATIONS),
                        "selected_duration_seconds": primary_horizon,
                        "primary_result_status": (
                            "UNRESOLVED"
                            if trade.get("primary_result_unresolved")
                            else "SAVED"
                        )
                    }
                )

                if comparison_saved:
                    trade["comparison_completed"] = True

                    logger.info(
                        "DURATION COMPARISON COMPLETED: %s",
                        symbol
                    )

            if trade.get("comparison_completed"):
                pending_trades.pop(symbol, None)

                logger.info(
                    "PAPER TRADE REMOVED FROM PENDING: %s",
                    symbol
                )
                    # ----------------------------------------------
    # MAIN LOOP
    # ----------------------------------------------

    try:
        while True:

            await process_control_commands()

            # --------------------------------------
            # SAFE STOP
            # --------------------------------------

            if stop_requested:
                if not pending_trades:
                    logger.info(
                        "STOP completed: no pending paper trades"
                    )

                    await send_status_message(
                        send_status,
                        "تم إيقاف الماسح بعد تسوية الصفقات التجريبية."
                    )
                    break

                elapsed = (
                    time.monotonic() - stop_requested_at
                    if stop_requested_at is not None
                    else 0
                )

                if elapsed >= STOP_SETTLEMENT_TIMEOUT:
                    logger.warning(
                        "SAFE STOP TIMEOUT: unresolved trades=%s",
                        list(pending_trades.keys())
                    )

                    for symbol, trade in list(
                        pending_trades.items()
                    ):
                        safe_record_update(
                            trade.get("history_id"),
                            "UNRESOLVED_ON_STOP",
                            {
                                "symbol": symbol,
                                "reason": "STOP_SETTLEMENT_TIMEOUT",
                                "entry_time": trade.get("entry_time"),
                                "entry_price": trade.get("entry_price"),
                                "selected_duration_seconds": trade.get(
                                    "selected_duration_seconds"
                                ),
                                "processed_horizons": sorted(
                                    trade.get(
                                        "processed_horizons",
                                        set()
                                    )
                                ),
                                "primary_result_saved": trade.get(
                                    "primary_result_saved",
                                    False
                                )
                            }
                        )

                        logger.warning(
                            "PAPER TRADE LEFT UNRESOLVED: %s | "
                            "no WIN/LOSS result was fabricated",
                            symbol
                        )

                        pending_trades.pop(symbol, None)

                    await send_status_message(
                        send_status,
                        "انتهت مهلة انتظار الصفقات التجريبية. "
                        "تم تسجيل الصفقات غير المحسومة دون احتساب "
                        "أي نتيجة ربح أو خسارة."
                    )

                    break

            # --------------------------------------
            # PERIODIC AUTO MARKET REFRESH
            # --------------------------------------

            if (
                not stop_requested
                and selection_mode == "auto"
                and time.monotonic() >= next_refresh
            ):
                next_refresh = (
                    time.monotonic()
                    + MARKET_REFRESH_SECONDS
                )

                try:
                    new_selection, new_histories = (
                        await asyncio.to_thread(
                            rank_otc_markets,
                            symbols
                        )
                    )

                    if new_selection:
                        if pending_alerts or pending_trades:
                            pending_selection = (
                                new_selection,
                                new_histories,
                                "auto"
                            )

                            logger.info(
                                "Market rotation postponed: "
                                "active alerts or trades"
                            )

                        else:
                            applied = await apply_selection(
                                new_selection,
                                new_histories,
                                "auto"
                            )

                            if not applied:
                                pending_selection = (
                                    new_selection,
                                    new_histories,
                                    "auto"
                                )

                    else:
                        logger.warning(
                            "Market refresh returned "
                            "no suitable markets"
                        )

                except Exception:
                    logger.exception(
                        "Dynamic market refresh failed"
                    )

            # --------------------------------------
            # APPLY POSTPONED MARKET SELECTION
            # --------------------------------------

            if (
                not stop_requested
                and pending_selection
                and not pending_alerts
                and not pending_trades
            ):
                (
                    new_selection,
                    new_histories,
                    new_mode
                ) = pending_selection

                applied = await apply_selection(
                    new_selection,
                    new_histories,
                    new_mode
                )

                if applied:
                    pending_selection = None

            # --------------------------------------
            # RECEIVE LIVE TICK
            # --------------------------------------

            try:
                tick = await asyncio.to_thread(
                    tick_queue.get,
                    True,
                    1
                )

            except queue.Empty:
                continue

            try:
                symbol = tick.get("symbol")

                if symbol not in symbols:
                    continue

                price = parse_number(tick.get("price"))
                raw_timestamp = parse_number(tick.get("time"))

                if price <= 0 or raw_timestamp <= 0:
                    continue

                timestamp = (
                    raw_timestamp
                    - OTC_TICK_TIME_OFFSET_SECONDS
                )

                if timestamp <= 0:
                    continue

                minute_bucket = int(timestamp // 60) * 60

                # ----------------------------------
                # PAPER TRADE RESULT PROCESSING
                # ----------------------------------

                trade = pending_trades.get(symbol)

                if trade:
                    await process_trade_results(
                        symbol,
                        trade,
                        timestamp,
                        price
                    )

                # ----------------------------------
                # BUILD LIVE ONE-MINUTE CANDLES
                # ----------------------------------

                candle = current_1m.get(symbol)

                if candle is None:
                    last_history_time = get_last_candle_time(
                        candles_1m[symbol]
                    )

                    expected_previous_time = minute_bucket - 60

                    if last_history_time != expected_previous_time:
                        logger.warning(
                            "INITIAL DATA ALIGNMENT NEEDED: %s | "
                            "expected=%s | actual=%s",
                            symbol,
                            expected_previous_time,
                            last_history_time
                        )

                        recovered = await asyncio.to_thread(
                            fetch_resynchronized_history,
                            symbol,
                            minute_bucket
                        )

                        if recovered is not None:
                            candles_1m[symbol] = recovered
                            data_ready[symbol] = True
                        else:
                            data_ready[symbol] = False

                    current_1m[symbol] = {
                        "time": minute_bucket,
                        "bucket": minute_bucket,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price
                    }

                    seconds_into_minute = (
                        timestamp - minute_bucket
                    )

                    current_1m_complete[symbol] = (
                        0 <= seconds_into_minute
                        <= MAX_CANDLE_START_DELAY
                    )

                    if not current_1m_complete[symbol]:
                        logger.warning(
                            "LIVE CANDLE STARTED LATE: %s | "
                            "delay=%.2fs | candle will not enter history",
                            symbol,
                            seconds_into_minute
                        )

                    continue

                if minute_bucket == candle["bucket"]:
                    candle["high"] = max(candle["high"], price)
                    candle["low"] = min(candle["low"], price)
                    candle["close"] = price

                elif minute_bucket > candle["bucket"]:

                    gap_seconds = (
                        minute_bucket - candle["bucket"]
                    )

                    if (
                        gap_seconds == 60
                        and data_ready.get(symbol, False)
                        and current_1m_complete.get(symbol, False)
                    ):
                        expected_previous_history = (
                            candle["bucket"] - 60
                        )

                        actual_previous_history = (
                            get_last_candle_time(candles_1m[symbol])
                        )

                        if (
                            actual_previous_history
                            == expected_previous_history
                        ):
                            completed_candle = dict(candle)
                            completed_candle["time"] = candle["bucket"]

                            add_candle(
                                candles_1m[symbol],
                                completed_candle
                            )

                        else:
                            logger.warning(
                                "CANDLE SEQUENCE MISMATCH: %s | "
                                "history=%s | expected=%s",
                                symbol,
                                actual_previous_history,
                                expected_previous_history
                            )

                            data_ready[symbol] = False

                    else:
                        if (
                            gap_seconds == 60
                            and not current_1m_complete.get(
                                symbol,
                                False
                            )
                        ):
                            logger.warning(
                                "PARTIAL CANDLE DISCARDED: %s | "
                                "bucket=%s | candle was not observed "
                                "from the beginning",
                                symbol,
                                candle["bucket"]
                            )

                        else:
                            missing_minutes = (
                                gap_seconds // 60
                            ) - 1

                            logger.warning(
                                "CANDLE GAP DETECTED: %s | "
                                "gap=%ss | missing_minutes=%s",
                                symbol,
                                gap_seconds,
                                missing_minutes
                            )

                        data_ready[symbol] = False

                    if not data_ready.get(symbol, False):
                        recovered = await asyncio.to_thread(
                            fetch_resynchronized_history,
                            symbol,
                            minute_bucket
                        )

                        if recovered is not None:
                            candles_1m[symbol] = recovered
                            data_ready[symbol] = True

                            logger.info(
                                "MARKET DATA RESTORED: %s",
                                symbol
                            )

                        else:
                            logger.warning(
                                "MARKET DATA NOT READY: %s | "
                                "signals temporarily disabled",
                                symbol
                            )

                    current_1m[symbol] = {
                        "time": minute_bucket,
                        "bucket": minute_bucket,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price
                    }

                    seconds_into_minute = (
                        timestamp - minute_bucket
                    )

                    current_1m_complete[symbol] = (
                        0 <= seconds_into_minute
                        <= MAX_CANDLE_START_DELAY
                    )

                    if not current_1m_complete[symbol]:
                        logger.warning(
                            "LIVE CANDLE STARTED LATE: %s | "
                            "delay=%.2fs | candle will not enter history",
                            symbol,
                            seconds_into_minute
                        )

                else:
                    continue

                # ----------------------------------
                # PENDING ALERT CONFIRMATION
                # ----------------------------------

                alert = pending_alerts.get(symbol)

                if alert:

                    if (
                        alert["status"] == "WAIT_CONFIRM"
                        and timestamp >= alert["confirm_at"]
                    ):

                        if timestamp >= alert["entry_at"]:
                            await cancel_alert(
                                symbol,
                                alert,
                                "وصل وقت الدخول قبل اكتمال التأكيد"
                            )
                            continue

                        if not data_ready.get(symbol, False):
                            await cancel_alert(
                                symbol,
                                alert,
                                "بيانات السوق غير مكتملة"
                            )
                            continue

                        valid, reason = validate_pending_signal(
                            alert,
                            price,
                            candles_1m[symbol],
                            current_1m.get(symbol),
                            timestamp
                        )

                        if valid:
                            message = dict(alert["signal"])
                            message["status"] = "ENTRY"

                            message["entry_time"] = format_time(
                                alert["entry_at"]
                            )

                            message["duration_seconds"] = alert[
                                "selected_duration_seconds"
                            ]

                            message["selected_duration_seconds"] = alert[
                                "selected_duration_seconds"
                            ]

                            message["duration_reason"] = alert[
                                "duration_reason"
                            ]

                            message["exit_time"] = format_time(
                                alert["entry_at"]
                                + alert["selected_duration_seconds"]
                            )

                            message["comparison_durations"] = list(
                                COMPARISON_DURATIONS
                            )

                            message["confirmation"] = reason

                            try:
                                delivered = await send_signal(message)
                            except Exception:
                                logger.exception(
                                    "Entry confirmation delivery failed: %s",
                                    symbol
                                )
                                delivered = False

                            if not delivered:
                                safe_record_update(
                                    alert.get("history_id"),
                                    "ENTRY_NOT_DELIVERED"
                                )

                                pending_alerts.pop(symbol, None)

                                logger.warning(
                                    "ENTRY CONFIRMATION NOT DELIVERED; "
                                    "PAPER TRADE BLOCKED: %s",
                                    symbol
                                )
                                continue

                            alert["status"] = "CONFIRMED"

                            safe_record_update(
                                alert.get("history_id"),
                                "CONFIRMED",
                                {
                                    "confirmation": reason,
                                    "confirmation_price": price,
                                    "planned_entry_time": alert["entry_at"],
                                    "selected_duration_seconds": alert[
                                        "selected_duration_seconds"
                                    ],
                                    "duration_reason": alert[
                                        "duration_reason"
                                    ]
                                }
                            )

                            logger.info(
                                "SIGNAL CONFIRMED AND DELIVERED: %s | %s | "
                                "duration=%ss",
                                symbol,
                                alert["signal"]["direction"],
                                alert["selected_duration_seconds"]
                            )

                        else:
                            await cancel_alert(
                                symbol,
                                alert,
                                reason
                            )
                            continue

                
                    # ----------------------------------
                    # START PAPER TRADE
                    # ----------------------------------

                    if (
                        alert
                        and alert["status"] == "CONFIRMED"
                        and timestamp >= alert["entry_at"]
                    ):

                        delay = timestamp - alert["entry_at"]

                        if delay > MAX_ENTRY_DELAY:
                            await cancel_alert(
                                symbol,
                                alert,
                                "تجاوز وقت الدخول المحدد"
                            )
                            continue

                        if symbol in pending_trades:
                            await cancel_alert(
                                symbol,
                                alert,
                                "توجد صفقة تجريبية مفتوحة"
                            )
                            continue

                        signal = alert["signal"]

                        selected_duration = alert[
                            "selected_duration_seconds"
                        ]

                        pending_trades[symbol] = {
                            "symbol": symbol,
                            "direction": signal["direction"],
                            "entry_price": price,
                            "entry_time": timestamp,
                            "strategies": list(
                                signal.get("matching_strategies", [])
                            ),
                            "strategy_reasons": list(
                                signal.get("strategy_reasons", [])
                            ),
                            "history_id": alert.get("history_id"),
                            "selected_duration_seconds": selected_duration,
                            "duration_reason": alert["duration_reason"],
                            "processed_horizons": set(),
                            "primary_result_saved": False,
                            "primary_result_unresolved": False,
                            "primary_result_delivered": False,
                            "primary_result_data": None,
                            "primary_result_details": None,
                            "comparison_completed": False
                        }

                        safe_record_update(
                            alert.get("history_id"),
                            "PAPER_TRADE_STARTED",
                            {
                                "entry_price": price,
                                "entry_time": timestamp,
                                "comparison_durations": list(
                                    COMPARISON_DURATIONS
                                ),
                                "selected_duration_seconds": selected_duration,
                                "duration_reason": alert["duration_reason"]
                            }
                        )

                        logger.info(
                            "PAPER TRADE STARTED: %s | %s | "
                            "Entry=%s | Selected=%ss | "
                            "Comparison=60s/120s",
                            symbol,
                            signal["direction"],
                            price,
                            selected_duration
                        )

                        pending_alerts.pop(symbol, None)
                        continue


                # ----------------------------------
                # DO NOT ANALYZE DURING OPEN TRADE
                # OR AFTER STOP REQUEST
                # ----------------------------------

                if stop_requested:
                    continue

                if symbol in pending_trades:
                    continue

                if symbol in pending_alerts:
                    continue

                # ----------------------------------
                # DATA INTEGRITY CHECK BEFORE ANALYSIS
                # ----------------------------------

                if not data_ready.get(symbol, False):
                    continue

                if len(candles_1m[symbol]) < MIN_CANDLES_1M:
                    continue

                if not has_recent_contiguous_candles(
                    candles_1m[symbol],
                    MIN_CANDLES_1M
                ):
                    data_ready[symbol] = False

                    logger.warning(
                        "ANALYSIS BLOCKED: %s | "
                        "recent candle sequence is not continuous",
                        symbol
                    )
                    continue

                # ----------------------------------
                # SIGNAL ANALYSIS
                # ----------------------------------

                signal = analyze_signal(
                    candles_1m[symbol],
                    symbol=symbol
                )

                if not signal:
                    continue

                if not is_new_signal(signal):
                    continue

                previous_time = last_signal_time.get(symbol, 0)

                if timestamp - previous_time < 60:
                    continue

                signal["symbol"] = symbol
                signal["price"] = price
                signal["analysis_timeframe"] = "1m"
                signal["status"] = "PRE_ALERT"

                (
                    selected_duration,
                    duration_reason
                ) = select_trade_duration(signal)

                signal["selected_duration_seconds"] = selected_duration
                signal["duration_seconds"] = selected_duration
                signal["duration_reason"] = duration_reason
                signal["expiry_minutes"] = selected_duration // 60

                # ----------------------------------
                # AI SHADOW PREDICTIONS
                # ----------------------------------

                signal["ai_shadow"] = evaluate_ai_shadow(signal)

                signal["ai_filter_shadow"] = evaluate_knn_shadow(signal)

                earliest_entry = timestamp + PRE_ENTRY_SECONDS

                entry_at = (
                    (int(earliest_entry) + 59) // 60
                ) * 60

                confirm_at = entry_at - CONFIRM_BEFORE_ENTRY

                alert = {
                    "signal": signal,
                    "entry_at": entry_at,
                    "confirm_at": confirm_at,
                    "status": "WAIT_CONFIRM",
                    "history_id": None,
                    "selected_duration_seconds": selected_duration,
                    "duration_reason": duration_reason
                }

                logger.info(
                    "SIGNAL FOUND: %s | %s | Score=%s | Strategies=%s | "
                    "Selected duration=%ss | Reason=%s | "
                    "Planned entry=%s | Confirmation=%s",
                    symbol,
                    signal.get("direction"),
                    signal.get("score"),
                    signal.get("matching_strategies", []),
                    selected_duration,
                    duration_reason,
                    format_time(entry_at),
                    format_time(confirm_at)
                )

                message = dict(signal)

                message["entry_time"] = format_time(entry_at)
                message["confirm_time"] = format_time(confirm_at)
                message["duration_seconds"] = selected_duration
                message["selected_duration_seconds"] = selected_duration
                message["duration_reason"] = duration_reason

                message["exit_time"] = format_time(
                    entry_at + selected_duration
                )

                # ----------------------------------
                # SAVE SIGNAL BEFORE PRE-ALERT
                # ----------------------------------

                try:
                    history_id = record_signal(signal)

                except Exception:
                    logger.exception(
                        "SIGNAL HISTORY SAVE ERROR: %s",
                        symbol
                    )
                    history_id = None

                if not history_id:
                    logger.error(
                        "SIGNAL HISTORY NOT SAVED: %s | "
                        "PRE-ALERT WILL NOT BE SENT",
                        symbol
                    )
                    continue

                alert["history_id"] = history_id

                safe_record_update(
                    history_id,
                    "PRE_ALERT_PENDING_DELIVERY",
                    {
                        "entry_at": entry_at,
                        "confirm_at": confirm_at,
                        "duration_seconds": selected_duration,
                        "selected_duration_seconds": selected_duration,
                        "duration_reason": duration_reason,
                        "comparison_durations": list(
                            COMPARISON_DURATIONS
                        )
                    }
                )

                # ----------------------------------
                # SAVE KNN SHADOW EVALUATION
                # ----------------------------------

                safe_record_update(
                    history_id,
                    "KNN_SHADOW_EVALUATED",
                    {
                        "model": "KNN_Shadow_v1",
                        "evaluation": signal.get(
                            "ai_filter_shadow",
                            {}
                        )
                    }
                )

                logger.info(
                    "KNN SHADOW SAVED: %s | history_id=%s",
                    symbol,
                    history_id
                )

                # ----------------------------------
                # SEND PRE-ALERT AFTER HISTORY SAVE
                # ----------------------------------

                try:
                    delivered = await send_signal(message)

                except Exception:
                    logger.exception(
                        "Pre-alert delivery failed for %s",
                        symbol
                    )
                    delivered = False

                if not delivered:
                    safe_record_update(
                        history_id,
                        "PRE_ALERT_NOT_DELIVERED"
                    )

                    logger.warning(
                        "PRE-ALERT NOT DELIVERED: %s | "
                        "signal saved but not activated",
                        symbol
                    )
                    continue

                safe_record_update(
                    history_id,
                    "PRE_ALERT_DELIVERED",
                    {
                        "entry_at": entry_at,
                        "confirm_at": confirm_at,
                        "duration_seconds": selected_duration,
                        "selected_duration_seconds": selected_duration,
                        "duration_reason": duration_reason,
                        "comparison_durations": list(
                            COMPARISON_DURATIONS
                        )
                    }
                )

                pending_alerts[symbol] = alert
                last_signal_time[symbol] = timestamp

                logger.info(
                    "PRE-ALERT DELIVERED: %s %s | "
                    "score=%s | duration=%ss | confirm=%s | entry=%s",
                    symbol,
                    signal["direction"],
                    signal.get("score"),
                    selected_duration,
                    format_time(confirm_at),
                    format_time(entry_at)
                )

            except Exception:
                logger.exception("Tick processing failed")

    finally:
        stop_event.set()

        await asyncio.to_thread(
            worker.join,
            5
        )

        if worker.is_alive():
            logger.warning(
                "OTC stream worker is still alive after shutdown timeout"
            )

        logger.info("OTC scanner stopped")