
import asyncio
import logging
import threading
import queue
import time
from datetime import datetime, timezone

from market_data import fetch_otc_symbols, fetch_candles, stream_ticks
from signal_engine import analyze_signal, is_new_signal


logger = logging.getLogger(__name__)

MAX_SYMBOLS = 5
TRADE_DURATION = 60

PRE_ENTRY_SECONDS = 60
CONFIRM_BEFORE_ENTRY = 15
MAX_ENTRY_DELAY = 10

HISTORY_1M = 300
MIN_CANDLES_1M = 60
MAX_CANDLES = 400


def format_time(timestamp):
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).strftime("%H:%M:%S UTC")


def stream_worker(symbols, tick_queue, stop_event):
    while not stop_event.is_set():
        try:
            logger.info("Connecting to OTC live stream")

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


def add_candle(candles, candle):
    candles.append(candle)

    if len(candles) > MAX_CANDLES:
        del candles[:-MAX_CANDLES]


def validate_pending_signal(alert, price, candles_1m):
    if len(candles_1m) < MIN_CANDLES_1M:
        return False, "بيانات الدقيقة غير كافية"

    original = alert["signal"]
    direction = original.get("direction")

    fresh_signal = analyze_signal(
        candles_1m,
        symbol=original.get("symbol")
    )

    if not fresh_signal:
        return False, "لم تعد توجد إشارة مؤكدة"

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

    original_price = float(original.get("price", price))

    if original_price <= 0:
        return False, "سعر الإشارة غير صالح"

    movement = abs(price - original_price) / original_price

    if movement > 0.002:
        return False, "تحرك السعر بعيداً عن نقطة الإشارة"

    return True, "تم تأكيد الإشارة"


async def scanner_loop(send_signal, send_result):
    logger.info("OTC Smart Analyzer starting")
    logger.info("Analysis timeframe: 1m only")

    symbols_data = await asyncio.to_thread(fetch_otc_symbols)

    if not symbols_data:
        logger.error("No OTC symbols received")
        return

    symbols_data = sorted(
        symbols_data,
        key=lambda item: float(item.get("payout") or 0),
        reverse=True
    )

    symbols_data = [
        item for item in symbols_data
        if item.get("isOtc") is True
        and item.get("open") is True
    ][:MAX_SYMBOLS]

    symbols = [item["symbol"] for item in symbols_data]

    if not symbols:
        logger.error("No open OTC symbols found")
        return

    logger.info("Monitoring symbols: %s", symbols)

    candles_1m = {}
    current_1m = {}

    for symbol in symbols:
        try:
            history = await asyncio.to_thread(
                fetch_candles,
                symbol,
                60,
                HISTORY_1M
            )

            candles_1m[symbol] = list(history)[-MAX_CANDLES:]

            logger.info(
                "History loaded: %s | 1m=%s",
                symbol,
                len(candles_1m[symbol])
            )

        except Exception:
            logger.exception(
                "History loading failed for %s",
                symbol
            )
            candles_1m[symbol] = []

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

    async def cancel_alert(symbol, alert, reason):
        message = dict(alert["signal"])
        message["status"] = "CANCELLED"
        message["reason"] = reason

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

    try:
        while True:
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

                price = float(tick.get("price") or 0)
                timestamp = float(tick.get("time") or 0)

                if price <= 0 or timestamp <= 0:
                    continue

                if symbol not in candles_1m:
                    continue

                minute_bucket = int(timestamp // 60) * 60

                # تسوية الصفقات التجريبية
                trade = pending_trades.get(symbol)

                if trade and timestamp >= trade["settles_at"]:
                    exit_price = price

                    if trade["direction"] == "CALL":
                        if exit_price > trade["entry_price"]:
                            result = "WIN"
                        elif exit_price < trade["entry_price"]:
                            result = "LOSS"
                        else:
                            result = "DRAW"
                    else:
                        if exit_price < trade["entry_price"]:
                            result = "WIN"
                        elif exit_price > trade["entry_price"]:
                            result = "LOSS"
                        else:
                            result = "DRAW"

                    result_data = {
                        "symbol": symbol,
                        "direction": trade["direction"],
                        "entry_price": trade["entry_price"],
                        "exit_price": exit_price,
                        "entry_time": format_time(
                            trade["entry_time"]
                        ),
                        "exit_time": format_time(timestamp),
                        "result": result,
                        "strategies": trade.get("strategies", []),
                        "strategy_reasons": trade.get(
                            "strategy_reasons", []
                        )
                    }

                    await send_result(result_data)

                    logger.info(
                        "PAPER RESULT: %s | %s | %s | Strategies=%s",
                        symbol,
                        trade["direction"],
                        result,
                        trade.get("strategies", [])
                    )

                    pending_trades.pop(symbol, None)

                # بناء شموع الدقيقة
                candle = current_1m.get(symbol)

                if candle is None:
                    current_1m[symbol] = {
                        "bucket": minute_bucket,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price
                    }
                    continue

                if minute_bucket == candle["bucket"]:
                    candle["high"] = max(candle["high"], price)
                    candle["low"] = min(candle["low"], price)
                    candle["close"] = price

                elif minute_bucket > candle["bucket"]:
                    if minute_bucket - candle["bucket"] == 60:
                        add_candle(
                            candles_1m[symbol],
                            dict(candle)
                        )
                    else:
                        logger.warning(
                            "Missing 1m candle: %s",
                            symbol
                        )

                    current_1m[symbol] = {
                        "bucket": minute_bucket,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price
                    }

                else:
                    continue

                # إعادة التحقق من الإشارة قبل الدخول
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

                        valid, reason = validate_pending_signal(
                            alert,
                            price,
                            candles_1m[symbol]
                        )

                        if valid:
                            message = dict(alert["signal"])
                            message["status"] = "ENTRY"
                            message["entry_time"] = format_time(
                                alert["entry_at"]
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
                                pending_alerts.pop(symbol, None)

                                logger.warning(
                                    "ENTRY CONFIRMATION NOT DELIVERED; "
                                    "PAPER TRADE BLOCKED: %s",
                                    symbol
                                )
                                continue

                            alert["status"] = "CONFIRMED"

                            logger.info(
                                "SIGNAL CONFIRMED AND DELIVERED: %s | %s",
                                symbol,
                                alert["signal"]["direction"]
                            )

                        else:
                            await cancel_alert(
                                symbol,
                                alert,
                                reason
                            )
                            continue

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

                        pending_trades[symbol] = {
                            "symbol": symbol,
                            "direction": signal["direction"],
                            "entry_price": price,
                            "entry_time": timestamp,
                            "settles_at": timestamp + TRADE_DURATION,
                            "strategies": list(
                                signal.get("matching_strategies", [])
                            ),
                            "strategy_reasons": list(
                                signal.get("strategy_reasons", [])
                            )
                        }

                        logger.info(
                            "PAPER TRADE STARTED: %s | %s | Strategies=%s",
                            symbol,
                            signal["direction"],
                            signal.get("matching_strategies", [])
                        )

                        pending_alerts.pop(symbol, None)
                        continue

                # لا نفتح تنبيهاً جديداً أثناء وجود صفقة أو تنبيه
                if symbol in pending_trades:
                    continue

                if symbol in pending_alerts:
                    continue

                if len(candles_1m[symbol]) < MIN_CANDLES_1M:
                    continue

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
                signal["expiry_minutes"] = 1
                signal["analysis_timeframe"] = "1m"
                signal["status"] = "PRE_ALERT"

                entry_at = timestamp + PRE_ENTRY_SECONDS
                confirm_at = entry_at - CONFIRM_BEFORE_ENTRY

                alert = {
                    "signal": signal,
                    "entry_at": entry_at,
                    "confirm_at": confirm_at,
                    "status": "WAIT_CONFIRM"
                }

                logger.info(
                    "SIGNAL FOUND: %s | %s | Strategies=%s",
                    symbol,
                    signal.get("direction"),
                    signal.get("matching_strategies", [])
                )

                message = dict(signal)
                message["entry_time"] = format_time(entry_at)
                message["confirm_time"] = format_time(confirm_at)

                try:
                    delivered = await send_signal(message)
                except Exception:
                    logger.exception(
                        "Pre-alert delivery failed for %s",
                        symbol
                    )
                    delivered = False

                if not delivered:
                    pending_alerts.pop(symbol, None)

                    logger.warning(
                        "PRE-ALERT NOT DELIVERED; SIGNAL DISCARDED: %s",
                        symbol
                    )
                    continue

                pending_alerts[symbol] = alert
                last_signal_time[symbol] = timestamp

                logger.info(
                    "PRE-ALERT DELIVERED: %s %s | confirm=%s | entry=%s",
                    symbol,
                    signal["direction"],
                    format_time(confirm_at),
                    format_time(entry_at)
                )

            except Exception:
                logger.exception("Tick processing failed")

    finally:
        stop_event.set()
        logger.info("OTC scanner stopped")
