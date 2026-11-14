
import asyncio
import logging
import threading
import queue
from datetime import datetime, timezone

from market_data import (
    fetch_otc_symbols,
    fetch_candles,
    stream_ticks
)

from signal_engine import (
    analyze_signal,
    is_new_signal,
    MIN_SCORE
)

logger = logging.getLogger(__name__)

# =====================================
# SETTINGS
# =====================================

MAX_SYMBOLS = 5

TRADE_DURATION = 60

PRE_ENTRY_SECONDS = 60
CONFIRM_BEFORE_ENTRY = 15
MAX_ENTRY_DELAY = 10

HISTORY_1M = 120
HISTORY_5M = 300

MIN_CANDLES_1M = 60
MIN_CANDLES_5M = 250

MAX_CANDLES = 400


# =====================================
# TIME
# =====================================

def format_time(timestamp):

    return datetime.fromtimestamp(
        timestamp,
        timezone.utc
    ).strftime("%H:%M:%S UTC")


# =====================================
# STREAM WORKER
# =====================================

def stream_worker(symbols, tick_queue, stop_event):

    while not stop_event.is_set():

        try:

            logger.info("Connecting to OTC live stream")

            received = False

            for tick in stream_ticks(symbols):

                if stop_event.is_set():
                    break

                if tick.get("_event") == "tick":

                    received = True

                    try:
                        tick_queue.put(tick, timeout=2)

                    except queue.Full:
                        logger.warning("Tick queue is full")

            if not received:
                logger.warning("Stream ended without ticks")

        except Exception:
            logger.exception("Live stream disconnected")

        if not stop_event.is_set():

            logger.info("Reconnecting in 5 seconds")
            stop_event.wait(5)


# =====================================
# CANDLE STORAGE
# =====================================

def add_candle(candles, candle):

    candles.append(candle)

    if len(candles) > MAX_CANDLES:
        del candles[:-MAX_CANDLES]


# =====================================
# SIGNAL VALIDATION
# =====================================

def validate_pending_signal(
    alert,
    price,
    candles_1m,
    candles_5m
):

    if (
        len(candles_1m) < MIN_CANDLES_1M
        or len(candles_5m) < MIN_CANDLES_5M
    ):
        return False, "بيانات الشموع غير كافية"

    original_direction = alert["signal"]["direction"]

    # إعادة تشغيل الاستراتيجية الكاملة
    fresh_signal = analyze_signal(
        candles_1m,
        candles_5m
    )

    if fresh_signal is None:
        return False, "شروط الاستراتيجية لم تعد مكتملة"

    if fresh_signal["direction"] != original_direction:
        return False, "اتجاه السوق تغير"

    if fresh_signal.get("score", 0) < MIN_SCORE:
        return False, "درجة الإشارة أقل من المطلوب"

    # =================================
    # PRICE MOVEMENT VALIDATION
    # =================================

    recent_ranges = [
        candle["high"] - candle["low"]
        for candle in candles_1m[-16:-1]
    ]

    if not recent_ranges:
        return False, "بيانات حركة السعر غير كافية"

    avg_range = sum(recent_ranges) / len(recent_ranges)

    if avg_range <= 0:
        return False, "متوسط حركة السعر غير صالح"

    alert_price = alert["alert_price"]

    if original_direction == "CALL":

        adverse_move = alert_price - price
        favorable_move = price - alert_price

    else:

        adverse_move = price - alert_price
        favorable_move = alert_price - price

    if adverse_move > avg_range * 0.75:
        return False, "السعر تحرك ضد الإشارة"

    if favorable_move > avg_range * 1.5:
        return False, "السعر ابتعد كثيراً عن منطقة الدخول"

    return True, "الاستراتيجية الجديدة ما زالت متوافقة"


# =====================================
# MAIN SCANNER
# =====================================

async def scanner_loop(send_signal, send_result):

    logger.info("OTC Smart Analyzer starting")

    # =================================
    # LOAD OTC SYMBOLS
    # =================================

    symbols_data = await asyncio.to_thread(
        fetch_otc_symbols
    )

    def payout_value(item):

        try:
            return float(item.get("payout") or 0)

        except (TypeError, ValueError):
            return 0

    symbols_data.sort(
        key=payout_value,
        reverse=True
    )

    selected = symbols_data[:MAX_SYMBOLS]

    symbols = [
        item["symbol"]
        for item in selected
    ]

    if not symbols:

        logger.error("No OTC symbols available")
        return

    logger.info("Monitoring symbols: %s", symbols)

    # =================================
    # LOAD HISTORICAL CANDLES
    # =================================

    candles_1m = {}
    candles_5m = {}

    for symbol in symbols:

        try:

            history_1m, history_5m = await asyncio.gather(

                asyncio.to_thread(
                    fetch_candles,
                    symbol,
                    60,
                    HISTORY_1M
                ),

                asyncio.to_thread(
                    fetch_candles,
                    symbol,
                    300,
                    HISTORY_5M
                )
            )

            candles_1m[symbol] = list(history_1m)[-MAX_CANDLES:]
            candles_5m[symbol] = list(history_5m)[-MAX_CANDLES:]

            logger.info(
                "History loaded: %s | 1m=%s | 5m=%s",
                symbol,
                len(candles_1m[symbol]),
                len(candles_5m[symbol])
            )

            if len(candles_5m[symbol]) < MIN_CANDLES_5M:

                logger.warning(
                    "%s has insufficient 5m history for EMA200",
                    symbol
                )

            if len(candles_1m[symbol]) < MIN_CANDLES_1M:

                logger.warning(
                    "%s has insufficient 1m history",
                    symbol
                )

        except Exception:

            logger.exception(
                "Could not load history for %s",
                symbol
            )

            candles_1m[symbol] = []
            candles_5m[symbol] = []

    # =================================
    # START LIVE STREAM
    # =================================

    tick_queue = queue.Queue(maxsize=5000)

    stop_event = threading.Event()

    worker = threading.Thread(
        target=stream_worker,
        args=(symbols, tick_queue, stop_event),
        daemon=True
    )

    worker.start()

    current_1m = {}
    building_5m = {}

    pending_alerts = {}
    pending_trades = {}

    # =================================
    # CANCEL ALERT
    # =================================

    async def cancel_alert(symbol, alert, reason):

        message = dict(alert["signal"])

        message.update({

            "symbol": symbol,

            "phase": "CANCELLED",

            "planned_entry_time": format_time(
                alert["entry_at"]
            ),

            "cancel_reason": reason

        })

        try:

            await send_signal(message)

        except Exception:

            logger.exception(
                "Cancellation notification failed"
            )

        logger.info(
            "Signal cancelled: %s | %s",
            symbol,
            reason
        )

    # =================================
    # PROCESS TICK
    # =================================

    async def process_tick(tick):

        symbol = tick.get("symbol")
        price = tick.get("price")
        timestamp = tick.get("time")

        if (
            symbol not in symbols
            or not isinstance(price, (int, float))
            or not isinstance(timestamp, (int, float))
        ):
            return

        price = float(price)
        timestamp = float(timestamp)

        # =================================
        # PAPER TRADE RESULTS
        # =================================

        trade = pending_trades.get(symbol)

        if trade and timestamp >= trade["settles_at"]:

            entry = trade["entry_price"]
            direction = trade["direction"]

            if price == entry:

                result = "DRAW"

            elif (
                direction == "CALL" and price > entry
            ) or (
                direction == "PUT" and price < entry
            ):

                result = "WIN"

            else:

                result = "LOSS"

            result_data = {

                "symbol": symbol,

                "direction": direction,

                "entry_price": entry,

                "exit_price": price,

                "entry_time": trade["entry_time"],

                "exit_time": timestamp,

                "result": result

            }

            del pending_trades[symbol]

            logger.info(
                "PAPER RESULT: %s %s",
                symbol,
                result
            )

            try:

                await send_result(result_data)

            except Exception:

                logger.exception(
                    "Result notification failed"
                )

        # =================================
        # BUILD 1M CANDLES
        # =================================

        minute_bucket = int(timestamp // 60)

        previous = current_1m.get(symbol)

        if previous is None:

            current_1m[symbol] = {

                "bucket": minute_bucket,

                "open": price,

                "high": price,

                "low": price,

                "close": price

            }

            return

        if minute_bucket < previous["bucket"]:
            return

        if minute_bucket == previous["bucket"]:

            previous["high"] = max(
                previous["high"],
                price
            )

            previous["low"] = min(
                previous["low"],
                price
            )

            previous["close"] = price

        else:

            if minute_bucket == previous["bucket"] + 1:

                closed_candle = {

                    "open": previous["open"],

                    "high": previous["high"],

                    "low": previous["low"],

                    "close": previous["close"]

                }

                add_candle(
                    candles_1m[symbol],
                    closed_candle
                )

                # =================================
                # BUILD 5M CANDLES
                # =================================

                five_bucket = previous["bucket"] // 5

                builder = building_5m.get(symbol)

                if builder is None:

                    building_5m[symbol] = {

                        "bucket": five_bucket,

                        "open": closed_candle["open"],

                        "high": closed_candle["high"],

                        "low": closed_candle["low"],

                        "close": closed_candle["close"],

                        "count": 1

                    }

                elif five_bucket == builder["bucket"]:

                    builder["high"] = max(
                        builder["high"],
                        closed_candle["high"]
                    )

                    builder["low"] = min(
                        builder["low"],
                        closed_candle["low"]
                    )

                    builder["close"] = closed_candle["close"]

                    builder["count"] += 1

                else:

                    if (
                        five_bucket == builder["bucket"] + 1
                        and builder["count"] == 5
                    ):

                        add_candle(

                            candles_5m[symbol],

                            {

                                "open": builder["open"],

                                "high": builder["high"],

                                "low": builder["low"],

                                "close": builder["close"]

                            }

                        )

                        logger.info(
                            "5m candle completed: %s",
                            symbol
                        )

                    building_5m[symbol] = {

                        "bucket": five_bucket,

                        "open": closed_candle["open"],

                        "high": closed_candle["high"],

                        "low": closed_candle["low"],

                        "close": closed_candle["close"],

                        "count": 1

                    }

            else:

                logger.warning(
                    "Missing minute data for %s",
                    symbol
                )

                building_5m.pop(symbol, None)

            current_1m[symbol] = {

                "bucket": minute_bucket,

                "open": price,

                "high": price,

                "low": price,

                "close": price

            }

        # =================================
        # PENDING ALERT VALIDATION
        # =================================

        alert = pending_alerts.get(symbol)

        if alert:

            # Confirm 15 seconds before entry
            if (
                alert["status"] == "WAIT_CONFIRM"
                and timestamp >= alert["confirm_at"]
            ):

                if timestamp >= alert["entry_at"]:

                    del pending_alerts[symbol]

                    await cancel_alert(
                        symbol,
                        alert,
                        "لم تصل بيانات التحقق قبل موعد الدخول"
                    )

                    return

                valid, reason = validate_pending_signal(

                    alert,

                    price,

                    candles_1m[symbol],

                    candles_5m[symbol]

                )

                if not valid:

                    del pending_alerts[symbol]

                    await cancel_alert(
                        symbol,
                        alert,
                        reason
                    )

                    return

                confirmation = dict(alert["signal"])

                confirmation.update({

                    "symbol": symbol,

                    "phase": "ENTRY",

                    "entry_time": format_time(
                        alert["entry_at"]
                    ),

                    "entry_price": "يُحدد عند موعد الدخول",

                    "expiry_minutes": 1

                })

                try:

                    delivered = await send_signal(
                        confirmation
                    )

                except Exception:

                    logger.exception(
                        "Confirmation delivery failed"
                    )

                    delivered = False

                if delivered:

                    alert["status"] = "CONFIRMED"

                    logger.info(
                        "ENTRY CONFIRMED: %s %s",
                        symbol,
                        alert["signal"]["direction"]
                    )

                else:

                    del pending_alerts[symbol]

                    logger.warning(
                        "Confirmation not delivered: %s",
                        symbol
                    )

                    return

            # =================================
            # PAPER ENTRY AT PLANNED TIME
            # =================================

            if (
                alert["status"] == "CONFIRMED"
                and timestamp >= alert["entry_at"]
            ):

                delay = timestamp - alert["entry_at"]

                if delay > MAX_ENTRY_DELAY:

                    del pending_alerts[symbol]

                    logger.warning(
                        "Entry missed: %s | delay=%.1fs",
                        symbol,
                        delay
                    )

                    return

                pending_trades[symbol] = {

                    "direction": alert["signal"]["direction"],

                    "entry_price": price,

                    "entry_time": timestamp,

                    "settles_at": timestamp + TRADE_DURATION

                }

                logger.info(
                    "PAPER TRADE STARTED: %s %s at %s",
                    symbol,
                    alert["signal"]["direction"],
                    price
                )

                del pending_alerts[symbol]

                return

            return

        # =================================
        # PREVENT OVERLAPPING TRADES
        # =================================

        if (
            symbol in pending_trades
            or len(candles_1m[symbol]) < MIN_CANDLES_1M
            or len(candles_5m[symbol]) < MIN_CANDLES_5M
        ):

            return

        # =================================
        # ANALYZE NEW SIGNAL
        # =================================

        signal = analyze_signal(

            candles_1m[symbol],

            candles_5m[symbol]

        )

        if signal is None:
            return

        if not is_new_signal(symbol, signal):
            return

        # =================================
        # CREATE PRE-ALERT
        # =================================

        entry_at = timestamp + PRE_ENTRY_SECONDS

        confirm_at = (
            entry_at - CONFIRM_BEFORE_ENTRY
        )

        pre_alert = dict(signal)

        pre_alert.update({

            "symbol": symbol,

            "phase": "PRE_ALERT",

            "alert_time": format_time(timestamp),

            "planned_entry_time": format_time(entry_at),

            "seconds_until_entry": PRE_ENTRY_SECONDS

        })

        try:

            delivered = await send_signal(pre_alert)

        except Exception:

            logger.exception(
                "Pre-alert delivery failed"
            )

            delivered = False

        if delivered:

            pending_alerts[symbol] = {

                "signal": signal,

                "alert_price": price,

                "confirm_at": confirm_at,

                "entry_at": entry_at,

                "status": "WAIT_CONFIRM"

            }

            logger.info(
                "PRE-ALERT SENT: %s %s | confirm=%s | entry=%s",
                symbol,
                signal["direction"],
                format_time(confirm_at),
                format_time(entry_at)
            )

        else:

            logger.warning(
                "Pre-alert not delivered: %s",
                symbol
            )

    # =====================================
    # MAIN LOOP
    # =====================================

    try:

        while True:

            try:

                tick = await asyncio.to_thread(
                    tick_queue.get,
                    True,
                    1
                )

                await process_tick(tick)

            except queue.Empty:

                continue

            except Exception:

                logger.exception(
                    "Tick processing failed"
                )

    finally:

        stop_event.set()

        logger.info("OTC scanner stopped")
