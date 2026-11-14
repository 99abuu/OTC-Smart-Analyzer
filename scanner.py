
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
SCAN_INTERVAL = 180
TRADE_DURATION = 60
TICK_MAX_AGE = 10


def scan_symbol(symbol):
    candles_1m = fetch_candles(
        symbol=symbol,
        timeframe=60,
        limit=50
    )

    candles_5m = fetch_candles(
        symbol=symbol,
        timeframe=300,
        limit=50
    )

    signal = analyze_signal(candles_1m, candles_5m)

    if signal and is_new_signal(symbol, signal):
        signal["symbol"] = symbol
        return signal

    return None


def stream_worker(symbols, tick_queue, stop_event):
    while not stop_event.is_set():
        try:
            logger.info("Connecting to OTC live stream")

            for tick in stream_ticks(symbols):
                if stop_event.is_set():
                    break

                if tick.get("_event") == "tick":
                    try:
                        tick_queue.put(tick, timeout=5)
                    except queue.Full:
                        logger.warning("Tick queue is full")

                elif tick.get("_event") in ("closed", "dropped"):
                    logger.warning("Stream event: %s", tick)

        except Exception:
            logger.exception("Live stream disconnected")

        if not stop_event.is_set():
            stop_event.wait(5)


def format_time(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        timezone.utc
    ).strftime("%H:%M:%S UTC")


async def scanner_loop(send_signal, send_result):
    logger.info("OTC scanner started")

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

    tick_queue = queue.Queue(maxsize=5000)
    stop_event = threading.Event()

    worker = threading.Thread(
        target=stream_worker,
        args=(symbols, tick_queue, stop_event),
        daemon=True
    )
    worker.start()

    latest_ticks = {}
    pending_trades = {}

    async def process_ticks():
        while True:
            tick = await asyncio.to_thread(tick_queue.get)

            symbol = tick.get("symbol")
            price = tick.get("price")
            timestamp = tick.get("time")

            if (
                symbol not in symbols
                or not isinstance(price, (int, float))
                or not isinstance(timestamp, (int, float))
            ):
                continue

            latest_ticks[symbol] = {
                "price": float(price),
                "time": float(timestamp),
                "received": time.monotonic()
            }

            trade = pending_trades.get(symbol)

            if trade and timestamp >= trade["settles_at"]:
                entry = trade["entry_price"]
                exit_price = float(price)
                direction = trade["direction"]

                if exit_price == entry:
                    result = "DRAW"
                elif (
                    direction == "CALL" and exit_price > entry
                ) or (
                    direction == "PUT" and exit_price < entry
                ):
                    result = "WIN"
                else:
                    result = "LOSS"

                result_data = {
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "entry_time": trade["entry_time"],
                    "exit_time": float(timestamp),
                    "result": result
                }

                del pending_trades[symbol]

                await send_result(result_data)

    tick_task = asyncio.create_task(process_ticks())

    try:
        while True:
            try:
                for item in selected:
                    symbol = item["symbol"]

                    if symbol in pending_trades:
                        continue

                    signal = await asyncio.to_thread(
                        scan_symbol,
                        symbol
                    )

                    if not signal:
                        continue

                    tick = latest_ticks.get(symbol)

                    if (
                        not tick
                        or time.monotonic() - tick["received"] > TICK_MAX_AGE
                    ):
                        logger.warning(
                            "Skipped stale signal for %s",
                            symbol
                        )
                        continue

                    entry_price = tick["price"]
                    entry_time = tick["time"]

                    trade = {
                        "symbol": symbol,
                        "direction": signal["direction"],
                        "entry_price": entry_price,
                        "entry_time": entry_time,
                        "settles_at": entry_time + TRADE_DURATION
                    }

                    pending_trades[symbol] = trade

                    signal["entry_price"] = entry_price
                    signal["entry_time"] = format_time(entry_time)
                    signal["expiry_minutes"] = 1

                    await send_signal(signal)

                    logger.info(
                        "Trade started: %s %s at %s",
                        symbol,
                        signal["direction"],
                        entry_price
                    )

            except Exception:
                logger.exception("Scanner cycle failed")

            await asyncio.sleep(SCAN_INTERVAL)

    finally:
        stop_event.set()
        tick_task.cancel()

        try:
            await tick_task
        except asyncio.CancelledError:
            pass
