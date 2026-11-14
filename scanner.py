
import asyncio
import logging
import threading
import queue
from datetime import datetime, timezone

from market_data import fetch_otc_symbols, fetch_candles, stream_ticks
from signal_engine import analyze_signal, is_new_signal

logger = logging.getLogger(__name__)

MAX_SYMBOLS = 5
TRADE_DURATION = 60
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

                elif tick.get("_event") in ("closed", "dropped"):
                    logger.warning("Stream event: %s", tick)

            if not received:
                logger.warning("Stream ended without receiving ticks")

        except Exception:
            logger.exception("Live stream disconnected")

        if not stop_event.is_set():
            logger.info("Reconnecting to live stream in 5 seconds")
            stop_event.wait(5)


def add_candle(candles, candle):
    candles.append(candle)

    if len(candles) > MAX_CANDLES:
        del candles[:-MAX_CANDLES]


async def scanner_loop(send_signal, send_result):
    logger.info("OTC continuous scanner starting")

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
    pending_trades = {}
    seen_tick_symbols = set()

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

        if symbol not in seen_tick_symbols:
            seen_tick_symbols.add(symbol)
            logger.info(
                "LIVE PRICE RECEIVED: %s = %s at %s",
                symbol,
                price,
                format_time(timestamp)
            )

        minute_bucket = int(timestamp // 60)

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

            await send_result(result_data)

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

        if minute_bucket == previous["bucket"]:
            previous["high"] = max(previous["high"], price)
            previous["low"] = min(previous["low"], price)
            previous["close"] = price
            return

        if minute_bucket < previous["bucket"]:
            return

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

            if (
                symbol not in pending_trades
                and len(candles_1m[symbol]) >= 25
                and len(candles_5m[symbol]) >= 25
            ):
                signal = analyze_signal(
                    candles_1m[symbol],
                    candles_5m[symbol]
                )

                if signal and is_new_signal(symbol, signal):
                    trade = {
                        "symbol": symbol,
                        "direction": signal["direction"],
                        "entry_price": price,
                        "entry_time": timestamp,
                        "settles_at": timestamp + TRADE_DURATION
                    }

                    pending_trades[symbol] = trade

                    signal["symbol"] = symbol
                    signal["entry_price"] = price
                    signal["entry_time"] = format_time(timestamp)
                    signal["expiry_minutes"] = 1

                    await send_signal(signal)

                    logger.info(
                        "Trade started: %s %s at %s",
                        symbol,
                        signal["direction"],
                        price
                    )

        else:
            logger.warning(
                "Missing minute data for %s; resetting 5m builder",
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
