
import os
import json
import urllib.request
import urllib.error
import urllib.parse


API_URL = "https://otcharts.com/v1/candles"
SYMBOLS_URL = "https://otcharts.com/v1/symbols"
STREAM_URL = "https://otcharts.com/v1/stream"


def get_api_key():
    api_key = os.getenv("OTCHARTS_API_KEY")

    if not api_key:
        raise RuntimeError("OTCHARTS_API_KEY is missing")

    return api_key


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
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as error:
        details = error.read().decode(
            "utf-8", errors="replace"
        )
        raise RuntimeError(
            f"OTCharts HTTP {error.code}: {details[:300]}"
        ) from None

    except urllib.error.URLError:
        raise RuntimeError(
            "Could not connect to OTCharts"
        ) from None


def fetch_candles(
    symbol="EURUSD_otc",
    timeframe=60,
    limit=50
):
    if timeframe not in (60, 300):
        raise ValueError("Only 1m and 5m are supported")

    params = urllib.parse.urlencode({
        "venue": "otc",
        "symbol": symbol,
        "tf": timeframe,
        "limit": limit
    })

    data = make_request(f"{API_URL}?{params}")
    candles = data.get("candles", [])

    if not candles:
        raise RuntimeError("No candles returned by OTCharts")

    required_fields = ("open", "high", "low", "close")

    for candle in candles:
        if not all(field in candle for field in required_fields):
            raise RuntimeError("Invalid candle data")

    return candles


def fetch_otc_symbols():
    params = urllib.parse.urlencode({
        "venue": "otc"
    })

    data = make_request(f"{SYMBOLS_URL}?{params}")

    if not isinstance(data, dict):
        raise RuntimeError("Invalid symbols response")

    symbols = data.get("symbols")

    if not isinstance(symbols, list):
        raise RuntimeError("Symbols list is missing")

    available_symbols = [
        item for item in symbols
        if isinstance(item, dict)
        and item.get("isOtc") is True
        and item.get("open") is True
        and isinstance(item.get("symbol"), str)
    ]

    return available_symbols


def stream_ticks(symbols):
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("A non-empty symbols list is required")

    if len(symbols) > 5:
        raise ValueError("Maximum 5 symbols per stream")

    if not all(isinstance(symbol, str) for symbol in symbols):
        raise ValueError("Every symbol must be a string")

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
        with urllib.request.urlopen(request, timeout=90) as response:
            event_name = "message"
            data_lines = []

            for raw_line in response:
                line = raw_line.decode(
                    "utf-8", errors="replace"
                ).strip()

                if not line:
                    if data_lines:
                        payload = json.loads("\n".join(data_lines))
                        payload["_event"] = event_name
                        yield payload

                    event_name = "message"
                    data_lines = []
                    continue

                if line.startswith("event:"):
                    event_name = line[6:].strip()

                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())

    except urllib.error.HTTPError as error:
        details = error.read().decode(
            "utf-8", errors="replace"
        )
        raise RuntimeError(
            f"OTCharts stream HTTP {error.code}: {details[:300]}"
        ) from None

    except urllib.error.URLError:
        raise RuntimeError(
            "Could not connect to OTCharts stream"
        ) from None
