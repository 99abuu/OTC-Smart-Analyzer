
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
from signal_engine import analyze_signal, is_new_signal

logger = logging.getLogger(__name__)

MAX_SYMBOLS = 5
TRADE_DURATION = 60
PRE_ENTRY_SECONDS = 60
MAX_CANDLES = 100


def format_time(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        timezone.utc
    ).strftime("%H:%M:%S UTC")


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


def add_candle(candles, candle):
    candles.append(candle)

    if len(candles) > MAX_CANDLES:
        del candles[:-MAX_CANDLES]


async def scanner_loop(send_signal, send_result):
    logger.info("OTC pre-entry scanner starting")

    symbols_data = await asyncio.to_thread(fetch_otc_symbols)

    symbols_data.sort(
        key=lambda item: float(item.get("payout") or 0),
        reverse=True
    )

    selected = symbols_data[:MAX_SYMBOLS]
    symbols = [item["symbol"] for item in selected]

    if not symbols:
        logger.error("No OTC symbols available")
        return

    logger.info("Monitoring: %s", symbols)

    candles_1m = {}
    candles_5m = {}

    for symbol in symbols:
        history_1m, history_5m = await asyncio.gather(
            asyncio.to_thread(fetch_candles, symbol, 60, 50),
            asyncio.to_thread(fetch_candles, symbol, 300, 50)
        )

        candles_1m[symbol] = list(history_1m)
        candles_5m[symbol] = list(history_5m)

        logger.info(
            "History loaded: %s | 1m=%s | 5m=%s",
            symbol,
            len(history_1m),
            len(history_5m)
        )

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

    async def notify_cancel(symbol, alert, reason):
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
            logger.exception("Cancellation notification failed")

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

        # تسوية الصفقات التي أُرسل إشعار دخولها بنجاح فقط
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
                logger.exception("Result notification failed")

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
            previous["high"] = max(previous["high"], price)
            previous["low"] = min(previous["low"], price)
            previous["close"] = price
            return

        # إغلاق شمعة الدقيقة السابقة
        if minute_bucket == previous["bucket"] + 1:
            closed_candle = {
                "open": previous["open"],
                "high": previous["high"],
                "low": previous["low"],
                "close": previous["close"]
            }

            add_candle(candles_1m[symbol], closed_candle)

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
                    builder["high"], closed_candle["high"]
                )
                builder["low"] = min(
                    builder["low"], closed_candle["low"]
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

        # تحديث الشمعة الحالية
        current_1m[symbol] = {
            "bucket": minute_bucket,
            "open": price,
            "high": price,
            "low": price,
            "close": price
        }

        # إعادة فحص الإشارة التي تنتظر موعد الدخول
        alert = pending_alerts.get(symbol)

        if alert and timestamp >= alert["entry_at"]:
            del pending_alerts[symbol]

            confirmed = analyze_signal(
                candles_1m[symbol],
                candles_5m[symbol]
            )

            if (
                confirmed is None
                or confirmed["direction"] != alert["signal"]["direction"]
            ):
                await notify_cancel(
                    symbol,
                    alert,
                    "تغيرت شروط السوق قبل الدخول"
                )
                logger.info("Signal cancelled: %s", symbol)
                return

            entry_message = dict(confirmed)
            entry_message.update({
                "symbol": symbol,
                "phase": "ENTRY",
                "entry_price": price,
                "entry_time": format_time(timestamp),
                "expiry_minutes": 1
            })

            # لا نسجل الصفقة إلا إذا وصل إشعار الدخول بنجاح
            try:
                delivered = await send_signal(entry_message)
            except Exception:
                logger.exception("Entry notification failed")
                delivered = False

            if delivered:
                pending_trades[symbol] = {
                    "direction": confirmed["direction"],
                    "entry_price": price,
                    "entry_time": timestamp,
                    "settles_at": timestamp + TRADE_DURATION
                }

                logger.info(
                    "Entry notification sent: %s %s",
                    symbol,
                    confirmed["direction"]
                )
            else:
                logger.warning(
                    "Entry not recorded because notification failed: %s",
                    symbol
                )

            return

        # لا نفتح تنبيهاً جديداً أثناء وجود تنبيه أو صفقة
        if (
            symbol in pending_alerts
            or symbol in pending_trades
            or len(candles_1m[symbol]) < 25
            or len(candles_5m[symbol]) < 25
        ):
            return

        signal = analyze_signal(
            candles_1m[symbol],
            candles_5m[symbol]
        )

        if not signal or not is_new_signal(symbol, signal):
            return

        planned_entry = timestamp + PRE_ENTRY_SECONDS

        pre_alert = dict(signal)
        pre_alert.update({
            "symbol": symbol,
            "phase": "PRE_ALERT",
            "alert_time": format_time(timestamp),
            "planned_entry_time": format_time(planned_entry),
            "seconds_until_entry": PRE_ENTRY_SECONDS
        })

        try:
            delivered = await send_signal(pre_alert)
        except Exception:
            logger.exception("Pre-entry notification failed")
            delivered = False

        if delivered:
            pending_alerts[symbol] = {
                "signal": signal,
                "entry_at": planned_entry
            }

            logger.info(
                "Pre-entry alert sent: %s %s",
                symbol,
                signal["direction"]
            )
        else:
            logger.warning(
                "Pre-entry alert was not delivered: %s",
                symbol
            )

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
                logger.exception("Tick processing failed")

    finally:
        stop_event.set()
        logger.info("OTC scanner stopped")
