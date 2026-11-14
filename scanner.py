
import asyncio
import logging

from market_data import fetch_otc_symbols, fetch_candles
from signal_engine import analyze_signal, is_new_signal


logger = logging.getLogger(__name__)

BATCH_SIZE = 5
SCAN_INTERVAL = 60


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


async def scan_all_symbols():
    symbols = await asyncio.to_thread(fetch_otc_symbols)

    if not symbols:
        logger.warning("No OTC symbols available")
        return []

    results = []

    for start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[start:start + BATCH_SIZE]

        for item in batch:
            symbol = item["symbol"]

            try:
                signal = await asyncio.to_thread(
                    scan_symbol,
                    symbol
                )

                if signal:
                    results.append(signal)

            except Exception as error:
                logger.error(
                    "Analysis failed for %s: %s",
                    symbol,
                    error
                )

        await asyncio.sleep(2)

    return results


async def scanner_loop(send_signal):
    logger.info("OTC scanner started")

    while True:
        try:
            signals = await scan_all_symbols()

            for signal in signals:
                await send_signal(signal)

        except Exception as error:
            logger.exception("Scanner cycle failed: %s", error)

        await asyncio.sleep(SCAN_INTERVAL)
