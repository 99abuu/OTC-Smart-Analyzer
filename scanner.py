
import asyncio
import logging

from market_data import fetch_otc_symbols, fetch_candles
from signal_engine import analyze_signal, is_new_signal


logger = logging.getLogger(__name__)

MAX_SYMBOLS = 5
SCAN_INTERVAL = 180


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

    # اختيار أعلى خمس أدوات من حيث Payout
    symbols.sort(
        key=lambda item: float(item.get("payout") or 0),
        reverse=True
    )

    selected = symbols[:MAX_SYMBOLS]

    if not selected:
        logger.warning("No OTC symbols available")
        return []

    logger.info(
        "Monitoring symbols: %s",
        [item["symbol"] for item in selected]
    )

    results = []

    for item in selected:
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

    return results


async def scanner_loop(send_signal):
    logger.info("OTC scanner started")

    while True:
        try:
            signals = await scan_all_symbols()

            for signal in signals:
                await send_signal(signal)

        except Exception as error:
            logger.exception(
                "Scanner cycle failed: %s",
                error
            )

        await asyncio.sleep(SCAN_INTERVAL)
