
import os
import json
import time
import math
import logging
import urllib.request
import urllib.error
import urllib.parse


logger = logging.getLogger(__name__)

API_URL = "https://otcharts.com/v1/candles"
SYMBOLS_URL = "https://otcharts.com/v1/symbols"
STREAM_URL = "https://otcharts.com/v1/stream"

REQUIRED_CANDLE_FIELDS = ("open", "high", "low", "close")


def get_api_key():
    api_key = os.getenv("OTCHARTS_API_KEY")

    if not api_key:
        raise RuntimeError("OTCHARTS_API_KEY is missing")

    return api_key


def parse_price(value):
    try:
        number = float(value)

        if not math.isfinite(number):
            return None

        return number

    except (TypeError, ValueError):
        return None


def validate_candle(candle):
    if not isinstance(candle, dict):
        return False

    if not all(
        field in candle
        for field in REQUIRED_CANDLE_FIELDS
    ):
        return False

    prices = {
        field: parse_price(candle.get(field))
        for field in REQUIRED_CANDLE_FIELDS
    }

    if any(value is None for value in prices.values()):
        return False

    open_price = prices["open"]
    high_price = prices["high"]
    low_price = prices["low"]
    close_price = prices["close"]

    if min(prices.values()) <= 0:
        return False

    if high_price < max(open_price, close_price, low_price):
        return False

    if low_price > min(open_price, close_price, high_price):
        return False

    return True


def make_request(url):
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {get_api_key()}",
            "Accept": "application/json",
            "User-Agent": "OTC-Smart-Analyzer/1.0"
        }
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(
                response.read().decode("utf-8")
            )

            if not isinstance(data, dict):
                raise RuntimeError(
                    "Invalid JSON response from OTCharts"
                )

            return data

    except urllib.error.HTTPError as error:
        details = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"OTCharts HTTP {error.code}: {details[:300]}"
        ) from None

    except urllib.error.URLError:
        raise RuntimeError(
            "Could not connect to OTCharts"
        ) from None

    except json.JSONDecodeError:
        raise RuntimeError(
            "OTCharts returned invalid JSON"
        ) from None


def fetch_candles(
    symbol="EURUSD_otc",
    timeframe=60,
    limit=50
):
    if timeframe not in (60, 300):
        raise ValueError(
            "Only 1m and 5m are supported"
        )

    if not isinstance(symbol, str) or not symbol:
        raise ValueError("Invalid symbol")

    if not isinstance(limit, int) or limit < 1:
        raise ValueError("Invalid candle limit")

    params = urllib.parse.urlencode({
        "venue": "otc",
        "symbol": symbol,
        "tf": timeframe,
        "limit": limit
    })

    data = make_request(
        f"{API_URL}?{params}"
    )

    candles = data.get("candles", [])

    if not isinstance(candles, list) or not candles:
        raise RuntimeError(
            "No candles returned by OTCharts"
        )

    invalid_count = 0

    for index, candle in enumerate(candles):
        if not validate_candle(candle):
            invalid_count += 1

            logger.error(
                "Invalid candle | symbol=%s | index=%s | candle=%r",
                symbol,
                index,
                candle
            )

    if invalid_count:
        raise RuntimeError(
            f"OTCharts returned {invalid_count} invalid candles "
            f"for {symbol}"
        )

    return candles


def fetch_otc_symbols():
    params = urllib.parse.urlencode({
        "venue": "otc"
    })

    data = make_request(
        f"{SYMBOLS_URL}?{params}"
    )

    symbols = data.get("symbols")

    if not isinstance(symbols, list):
        raise RuntimeError(
            "Symbols list is missing"
        )

    available_symbols = [
        item for item in symbols
        if isinstance(item, dict)
        and item.get("isOtc") is True
        and item.get("open") is True
        and isinstance(item.get("symbol"), str)
        and item.get("symbol").strip()
    ]

    if not available_symbols:
        raise RuntimeError(
            "No open OTC symbols returned by OTCharts"
        )

    return available_symbols


def diagnose_tick_time(payload, diagnosed_symbols):
    symbol = payload.get("symbol")

    if not isinstance(symbol, str):
        return

    if symbol in diagnosed_symbols:
        return

    raw_tick_time = payload.get("time")
    server_time = time.time()

    try:
        tick_time = float(raw_tick_time)

        if not math.isfinite(tick_time):
            raise ValueError("Non-finite timestamp")

        difference = tick_time - server_time

    except (TypeError, ValueError):
        difference = None

    logger.info(
        "TIME DIAGNOSTIC | symbol=%s | "
        "raw_tick_time=%r | server_time=%.3f | "
        "difference_seconds=%s",
        symbol,
        raw_tick_time,
        server_time,
        (
            round(difference, 3)
            if difference is not None
            else "INVALID_TIMESTAMP"
        )
    )

    diagnosed_symbols.add(symbol)


def decode_stream_event(event_name, data_lines):
    if not data_lines:
        return None

    raw_data = "\n".join(data_lines)

    try:
        payload = json.loads(raw_data)

    except json.JSONDecodeError:
        logger.warning(
            "Invalid SSE JSON message | event=%s | data=%s",
            event_name,
            raw_data[:300]
        )
        return None

    if not isinstance(payload, dict):
        logger.warning(
            "Unexpected SSE payload type | event=%s | type=%s",
            event_name,
            type(payload).__name__
        )
        return None

    payload["_event"] = event_name or "message"

    return payload


def stream_ticks(symbols):
    if not isinstance(symbols, list) or not symbols:
        raise ValueError(
            "A non-empty symbols list is required"
        )

    if len(symbols) > 5:
        raise ValueError(
            "Maximum 5 symbols per stream"
        )

    if not all(
        isinstance(symbol, str) and symbol.strip()
        for symbol in symbols
    ):
        raise ValueError(
            "Every symbol must be a non-empty string"
        )

    params = urllib.parse.urlencode({
        "venue": "otc",
        "symbol": ",".join(symbols)
    })

    request = urllib.request.Request(
        f"{STREAM_URL}?{params}",
        headers={
            "Authorization": f"Bearer {get_api_key()}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": "OTC-Smart-Analyzer/1.0"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=90
        ) as response:

            event_name = "message"
            data_lines = []
            diagnosed_symbols = set()

            def process_event():
                nonlocal event_name, data_lines

                payload = decode_stream_event(
                    event_name,
                    data_lines
                )

                event_name = "message"
                data_lines = []

                if payload is None:
                    return None

                if payload.get("_event") == "tick":
                    diagnose_tick_time(
                        payload,
                        diagnosed_symbols
                    )

                return payload

            for raw_line in response:
                line = raw_line.decode(
                    "utf-8",
                    errors="replace"
                ).rstrip("\r\n")

                if line == "":
                    payload = process_event()

                    if payload is not None:
                        yield payload

                    continue

                # SSE comment / heartbeat
                if line.startswith(":"):
                    continue

                if line.startswith("event:"):
                    event_name = line[6:].strip()

                elif line.startswith("data:"):
                    data_lines.append(
                        line[5:].lstrip()
                    )

                # Ignore other SSE fields:
                # id:, retry:, etc.

            # Process the final event if the connection
            # closes without an empty line separator.
            if data_lines:
                payload = process_event()

                if payload is not None:
                    yield payload

    except urllib.error.HTTPError as error:
        details = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"OTCharts stream HTTP {error.code}: {details[:300]}"
        ) from None

    except urllib.error.URLError:
        raise RuntimeError(
            "Could not connect to OTCharts stream"
        ) from None
