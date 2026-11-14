
import os
import json
import urllib.request
import urllib.error
import urllib.parse


API_URL = "https://otcharts.com/v1/candles"


def fetch_candles(symbol="EURUSD_otc", timeframe=60, limit=50):
    api_key = os.getenv("OTCHARTS_API_KEY")

    if not api_key:
        raise RuntimeError("OTCHARTS_API_KEY is missing")

    if timeframe not in (60, 300):
        raise ValueError("Only 1m and 5m are supported")

    params = urllib.parse.urlencode({
        "venue": "otc",
        "symbol": symbol,
        "tf": timeframe,
        "limit": limit
    })

    request = urllib.request.Request(
        f"{API_URL}?{params}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"OTCharts API error: HTTP {error.code}"
        ) from None

    except urllib.error.URLError:
        raise RuntimeError("Could not connect to OTCharts") from None

    candles = data.get("candles", [])

    if not candles:
        raise RuntimeError("No candles returned by OTCharts")

    required_fields = ("open", "high", "low", "close")

    for candle in candles:
        if not all(field in candle for field in required_fields):
            raise RuntimeError("Invalid candle data")

    return candles
