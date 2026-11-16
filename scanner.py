
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

LOCAL_TIMEZONE = ZoneInfo("Europe/Nicosia")


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
    ]

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

    return round(final_score, 2)


# --------------------------------------------------
# MARKET SELECTION
# --------------------------------------------------

def rank_otc_markets(current_symbols=None):
    current_symbols = current_symbols or []

    symbols_data = fetch_otc_symbols()

    if not symbols_data:
        return [], {}

    open_symbols = [
        item for item in symbols_data
        if isinstance(item, dict)
        and item.get("isOtc") is True
        and item.get("open") is True
        and item.get("symbol")
    ]

    open_symbols.sort(
        key=lambda item: parse_number(
            item.get("payout")
        ),
        reverse=True
    )

    candidates = []
    seen = set()

    by_symbol = {
        item["symbol"]: item
        for item in open_symbols
    }

    for symbol in current_symbols:
        if symbol in by_symbol and symbol not in seen:
            candidates.append(by_symbol[symbol])
            seen.add(symbol)

    for item in open_symbols:
        symbol = item["symbol"]

        if symbol in seen:
            continue

        candidates.append(item)
        seen.add(symbol)

        if len(candidates) >= MAX_CANDIDATES:
            break

    ranked = []
    histories = {}

    current_bucket = int(time.time() // 60) * 60

    for item in candidates:
        symbol = item["symbol"]

        try:
            raw_candles = fetch_candles(
                symbol,
                60,
                HISTORY_1M
            )

            candles = normalize_api_candles(
                raw_candles
            )

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

            payout = parse_number(
                item.get("payout")
            )

            score = market_score(
                candles,
                payout
            )

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
        key=lambda item: item["score"],
        reverse=True
    )

    return ranked[:MAX_SYMBOLS], histories


# --------------------------------------------------
# HISTORY RESYNCHRONIZATION
# --------------------------------------------------

def fetch_resynchronized_history(
    symbol,
    current_bucket
):
    try:
        raw_candles = fetch_candles(
            symbol,
            60,
            HISTORY_1M
        )

        candles = normalize_api_candles(
            raw_candles
        )

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
        logger.exception(
            "RESYNC ERROR: %s",
            symbol
        )
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

    validated_live = valid_market_candles(
        [live_snapshot]
    )

    if len(validated_live) != 1:
        return False, "بيانات الشمعة الحية غير صالحة"

    original = alert["signal"]
    direction = original.get("direction")

    # استخدام الشموع المكتملة فقط، كما في التحليل الأصلي.
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

async def scanner_loop(send_signal, send_result):
    logger.info("OTC Smart Analyzer starting")
    logger.info("Analysis timeframe: 1m only")
    logger.info("Dynamic market selection enabled")
    logger.info("Trade history recording enabled")

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

    logger.info("Display timezone: Europe/Nicosia")

    selected, initial_histories = await asyncio.to_thread(
        rank_otc_markets,
        []
    )

    if not selected:
        logger.error("No suitable OTC markets found")
        return

    symbols = [
        item["symbol"]
        for item in selected
    ]

    logger.info(
        "Selected OTC markets: %s",
        [
            (item["symbol"], item["score"])
            for item in selected
        ]
    )

    candles_1m = {
        symbol: initial_histories.get(symbol, [])
        for symbol in symbols
    }

    current_1m = {}

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

    next_refresh = (
        time.monotonic() + MARKET_REFRESH_SECONDS
    )

    async def cancel_alert(symbol, alert, reason):
        message = dict(alert["signal"])
        message["status"] = "CANCELLED"
        message["reason"] = reason

        record_update(
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

    async def apply_selection(
        new_selection,
        new_histories
    ):
        nonlocal symbols
        nonlocal tick_queue
        nonlocal stop_event
        nonlocal worker
        nonlocal candles_1m
        nonlocal current_1m
        nonlocal data_ready

        new_symbols = [
            item["symbol"]
            for item in new_selection
        ]

        if not new_symbols:
            return False

        if new_symbols == symbols:
            logger.info(
                "Market selection unchanged: %s",
                symbols
            )
            return True

        if pending_alerts or pending_trades:
            return False

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
                "market rotation postponed"
            )
            return False

        symbols = new_symbols

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

        tick_queue = queue.Queue()
        stop_event = threading.Event()

        worker = threading.Thread(
            target=stream_worker,
            args=(symbols, tick_queue, stop_event),
            daemon=True
        )

        worker.start()

        logger.info(
            "New OTC markets active: %s",
            [
                (item["symbol"], item["score"])
                for item in new_selection
            ]
        )

        return True

    try:
        while True:

            # --------------------------------------
            # PERIODIC MARKET REFRESH
            # --------------------------------------

            if time.monotonic() >= next_refresh:
                next_refresh = (
                    time.monotonic()
                    + MARKET_REFRESH_SECONDS
                )

                try:
                    (
                        new_selection,
                        new_histories
                    ) = await asyncio.to_thread(
                        rank_otc_markets,
                        symbols
                    )

                    if new_selection:
                        if pending_alerts or pending_trades:
                            pending_selection = (
                                new_selection,
                                new_histories
                            )

                            logger.info(
                                "Market rotation postponed: "
                                "active alerts or trades"
                            )

                        else:
                            applied = await apply_selection(
                                new_selection,
                                new_histories
                            )

                            if not applied:
                                pending_selection = (
                                    new_selection,
                                    new_histories
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
                pending_selection
                and not pending_alerts
                and not pending_trades
            ):
                (
                    new_selection,
                    new_histories
                ) = pending_selection

                applied = await apply_selection(
                    new_selection,
                    new_histories
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

                price = parse_number(
                    tick.get("price")
                )

                raw_timestamp = parse_number(
                    tick.get("time")
                )

                if price <= 0 or raw_timestamp <= 0:
                    continue

                timestamp = (
                    raw_timestamp
                    - OTC_TICK_TIME_OFFSET_SECONDS
                )

                if timestamp <= 0:
                    continue

                minute_bucket = (
                    int(timestamp // 60) * 60
                )

                # ----------------------------------
                # PAPER TRADE RESULT PROCESSING
                # ----------------------------------

                trade = pending_trades.get(symbol)

                if trade:
                    for horizon in COMPARISON_DURATIONS:

                        if horizon in trade["processed_horizons"]:
                            continue

                        target_time = (
                            trade["entry_time"] + horizon
                        )

                        if timestamp < target_time:
                            continue

                        delay = timestamp - target_time

                        if delay > MAX_HORIZON_DELAY:
                            record_update(
                                trade.get("history_id"),
                                "HORIZON_RESULT_MISSED",
                                {
                                    "horizon_seconds": horizon,
                                    "target_time": target_time,
                                    "observed_time": timestamp,
                                    "delay_seconds": delay
                                }
                            )

                            logger.warning(
                                "HORIZON RESULT MISSED: %s | "
                                "%ss | tick delayed by %ss",
                                symbol,
                                horizon,
                                delay
                            )

                            trade["processed_horizons"].add(
                                horizon
                            )

                            continue

                        outcome = calculate_outcome(
                            trade["direction"],
                            trade["entry_price"],
                            price
                        )

                        horizon_details = {
                            "symbol": symbol,
                            "direction": trade["direction"],
                            "strategies": trade.get(
                                "strategies",
                                []
                            ),
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

                        saved = record_horizon_result(
                            trade.get("history_id"),
                            horizon,
                            outcome,
                            entry_price=trade["entry_price"],
                            exit_price=price,
                            details=horizon_details
                        )

                        if saved:
                            trade["processed_horizons"].add(
                                horizon
                            )

                            logger.info(
                                "HORIZON RESULT: %s | %ss | %s | "
                                "entry=%s | exit=%s",
                                symbol,
                                horizon,
                                outcome,
                                trade["entry_price"],
                                price
                            )

                        else:
                            logger.error(
                                "HORIZON RESULT SAVE FAILED: %s | %ss",
                                symbol,
                                horizon
                            )

                        if (
                            horizon
                            == trade["selected_duration_seconds"]
                            and saved
                            and not trade.get("primary_result_saved")
                        ):
                            result_data = {
                                "symbol": symbol,
                                "direction": trade["direction"],
                                "entry_price": trade["entry_price"],
                                "exit_price": price,
                                "entry_time": format_time(
                                    trade["entry_time"]
                                ),
                                "exit_time": format_time(
                                    timestamp
                                ),
                                "result": outcome,
                                "duration_seconds": horizon,
                                "duration_reason": trade.get(
                                    "duration_reason",
                                    ""
                                ),
                                "strategies": trade.get(
                                    "strategies",
                                    []
                                ),
                                "strategy_reasons": trade.get(
                                    "strategy_reasons",
                                    []
                                )
                            }

                            primary_saved = record_result(
                                trade.get("history_id"),
                                outcome,
                                entry_price=trade["entry_price"],
                                exit_price=price,
                                details=horizon_details
                            )

                            if primary_saved:
                                trade["primary_result_saved"] = True

                                try:
                                    await send_result(result_data)

                                except Exception:
                                    logger.exception(
                                        "Selected-duration result "
                                        "delivery failed: %s",
                                        symbol
                                    )

                                logger.info(
                                    "PAPER RESULT %ss: %s | %s | %s | "
                                    "Strategies=%s",
                                    horizon,
                                    symbol,
                                    trade["direction"],
                                    outcome,
                                    trade.get("strategies", [])
                                )

                            else:
                                logger.error(
                                    "PRIMARY RESULT SAVE FAILED: %s",
                                    symbol
                                )

                    if all(
                        horizon in trade["processed_horizons"]
                        for horizon in COMPARISON_DURATIONS
                    ):
                        record_update(
                            trade.get("history_id"),
                            "DURATION_COMPARISON_COMPLETED",
                            {
                                "durations": list(
                                    COMPARISON_DURATIONS
                                ),
                                "selected_duration_seconds": trade[
                                    "selected_duration_seconds"
                                ]
                            }
                        )

                        logger.info(
                            "DURATION COMPARISON COMPLETED: %s",
                            symbol
                        )

                        pending_trades.pop(
                            symbol,
                            None
                        )

                # ----------------------------------
                # BUILD LIVE ONE-MINUTE CANDLES
                # ----------------------------------

                candle = current_1m.get(symbol)

                if candle is None:
                    last_history_time = get_last_candle_time(
                        candles_1m[symbol]
                    )

                    expected_previous_time = (
                        minute_bucket - 60
                    )

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

                    continue

                if minute_bucket == candle["bucket"]:
                    candle["high"] = max(
                        candle["high"],
                        price
                    )

                    candle["low"] = min(
                        candle["low"],
                        price
                    )

                    candle["close"] = price

                elif minute_bucket > candle["bucket"]:

                    gap_seconds = (
                        minute_bucket - candle["bucket"]
                    )

                    if gap_seconds == 60 and data_ready.get(symbol, False):

                        expected_previous_history = (
                            candle["bucket"] - 60
                        )

                        actual_previous_history = (
                            get_last_candle_time(
                                candles_1m[symbol]
                            )
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
                                record_update(
                                    alert.get("history_id"),
                                    "ENTRY_NOT_DELIVERED"
                                )

                                pending_alerts.pop(
                                    symbol,
                                    None
                                )

                                logger.warning(
                                    "ENTRY CONFIRMATION NOT DELIVERED; "
                                    "PAPER TRADE BLOCKED: %s",
                                    symbol
                                )

                                continue

                            alert["status"] = "CONFIRMED"

                            record_update(
                                alert.get("history_id"),
                                "CONFIRMED",
                                {
                                    "confirmation": reason,
                                    "confirmation_price": price,
                                    "planned_entry_time": alert[
                                        "entry_at"
                                    ],
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

                        delay = (
                            timestamp - alert["entry_at"]
                        )

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
                                signal.get(
                                    "matching_strategies",
                                    []
                                )
                            ),
                            "strategy_reasons": list(
                                signal.get(
                                    "strategy_reasons",
                                    []
                                )
                            ),
                            "history_id": alert.get("history_id"),
                            "selected_duration_seconds": selected_duration,
                            "duration_reason": alert["duration_reason"],
                            "processed_horizons": set(),
                            "primary_result_saved": False
                        }

                        record_update(
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

                        pending_alerts.pop(
                            symbol,
                            None
                        )

                        continue

                # ----------------------------------
                # DO NOT ANALYZE DURING OPEN TRADE
                # ----------------------------------

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

                previous_time = last_signal_time.get(
                    symbol,
                    0
                )

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

                signal["selected_duration_seconds"] = (
                    selected_duration
                )

                signal["duration_seconds"] = (
                    selected_duration
                )

                signal["duration_reason"] = duration_reason

                signal["expiry_minutes"] = (
                    selected_duration // 60
                )

                earliest_entry = (
                    timestamp + PRE_ENTRY_SECONDS
                )

                entry_at = (
                    (int(earliest_entry) + 59) // 60
                ) * 60

                confirm_at = (
                    entry_at - CONFIRM_BEFORE_ENTRY
                )

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
                    "SIGNAL FOUND: %s | %s | Strategies=%s | "
                    "Selected duration=%ss | Reason=%s | "
                    "Planned entry=%s | Confirmation=%s",
                    symbol,
                    signal.get("direction"),
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

                try:
                    delivered = await send_signal(message)

                except Exception:
                    logger.exception(
                        "Pre-alert delivery failed for %s",
                        symbol
                    )
                    delivered = False

                if not delivered:
                    logger.warning(
                        "PRE-ALERT NOT DELIVERED; "
                        "SIGNAL DISCARDED: %s",
                        symbol
                    )
                    continue

                history_id = record_signal(signal)
                alert["history_id"] = history_id

                if history_id:
                    record_update(
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

                else:
                    logger.error(
                        "SIGNAL HISTORY NOT SAVED: %s | "
                        "signal will not be activated",
                        symbol
                    )
                    continue

                pending_alerts[symbol] = alert
                last_signal_time[symbol] = timestamp

                logger.info(
                    "PRE-ALERT DELIVERED: %s %s | "
                    "duration=%ss | confirm=%s | entry=%s",
                    symbol,
                    signal["direction"],
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

        logger.info("OTC scanner stopped")
