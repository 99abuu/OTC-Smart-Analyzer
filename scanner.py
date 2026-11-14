
import asyncio
import logging
import threading
import queue
import time
from datetime import datetime, timezone
from statistics import mean

from market_data import fetch_otc_symbols, fetch_candles, stream_ticks
from signal_engine import analyze_signal, is_new_signal

from trade_history import (
    record_signal,
    record_update,
    record_result
)


logger = logging.getLogger(__name__)

MAX_SYMBOLS = 5
MAX_CANDIDATES = 18
MARKET_REFRESH_SECONDS = 300

TRADE_DURATION = 60
PRE_ENTRY_SECONDS = 60
CONFIRM_BEFORE_ENTRY = 15
MAX_ENTRY_DELAY = 10

HISTORY_1M = 80
MIN_CANDLES_1M = 60
MAX_CANDLES = 400


def format_time(timestamp):
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).strftime("%H:%M:%S UTC")


def parse_number(value, default=0):
    try:
        if isinstance(value, str):
            value = value.replace("%", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def valid_market_candles(candles):
    result = []

    if not isinstance(candles, list):
        return result

    for candle in candles:
        if not isinstance(candle, dict):
            continue

        try:
            o = float(candle["open"])
            h = float(candle["high"])
            l = float(candle["low"])
            c = float(candle["close"])

            if min(o, h, l, c) <= 0:
                continue

            if h < max(o, c, l) or l > min(o, c, h):
                continue

            result.append({
                "open": o,
                "high": h,
                "low": l,
                "close": c
            })

        except (KeyError, TypeError, ValueError):
            continue

    return result


def market_score(candles, payout):
    candles = valid_market_candles(candles)

    if len(candles) < MIN_CANDLES_1M:
        return 0

    recent = candles[-60:]

    ranges = [
        (c["high"] - c["low"]) / c["close"] * 100
        for c in recent
    ]

    average_range = mean(ranges)

    volatility_score = max(
        0,
        100 - abs(average_range - 0.12) / 0.20 * 100
    )

    closes = [c["close"] for c in recent]

    movements = [
        abs(closes[i] - closes[i - 1])
        for i in range(1, len(closes))
    ]

    total_movement = sum(movements)

    if total_movement > 0:
        consistency = (
            abs(closes[-1] - closes[0]) / total_movement
        ) * 100
    else:
        consistency = 0

    trend_score = min(100, consistency)

    average_atr = mean([
        c["high"] - c["low"] for c in recent
    ])

    tolerance = max(
        average_atr * 0.30,
        closes[-1] * 0.0001
    )

    pivot_highs = []
    pivot_lows = []

    for i in range(2, len(recent) - 2):
        high = recent[i]["high"]
        low = recent[i]["low"]

        if all(
            high >= recent[j]["high"]
            for j in range(i - 2, i + 3) if j != i
        ):
            pivot_highs.append(high)

        if all(
            low <= recent[j]["low"]
            for j in range(i - 2, i + 3) if j != i
        ):
            pivot_lows.append(low)

    def count_level_groups(levels):
        groups = []

        for level in levels:
            matched = False

            for group in groups:
                if abs(level - group[0]) <= tolerance:
                    group.append(level)
                    matched = True
                    break

            if not matched:
                groups.append([level])

        return sum(
            1 for group in groups if len(group) >= 2
        )

    level_groups = (
        count_level_groups(pivot_highs)
        + count_level_groups(pivot_lows)
    )

    support_resistance_score = min(
        100, level_groups * 20
    )

    data_score = min(
        100, len(candles) / HISTORY_1M * 100
    )

    payout_score = max(0, min(100, payout))

    final_score = (
        volatility_score * 0.25
        + consistency * 0.20
        + support_resistance_score * 0.15
        + data_score * 0.20
        + trend_score * 0.10
        + payout_score * 0.10
    )

    return round(final_score, 2)


def rank_otc_markets(current_symbols=None):
    current_symbols = current_symbols or []

    symbols_data = fetch_otc_symbols()

    if not symbols_data:
        return [], {}

    open_symbols = [
        item for item in symbols_data
        if isinstance(item, dict)
        and item.get("isOtc") is True
        and item.get("open") is True
        and item.get("symbol")
    ]

    open_symbols.sort(
        key=lambda item: parse_number(item.get("payout")),
        reverse=True
    )

    candidates = []
    seen = set()

    by_symbol = {
        item["symbol"]: item for item in open_symbols
    }

    for symbol in current_symbols:
        if symbol in by_symbol and symbol not in seen:
            candidates.append(by_symbol[symbol])
            seen.add(symbol)

    for item in open_symbols:
        symbol = item["symbol"]

        if symbol in seen:
            continue

        candidates.append(item)
        seen.add(symbol)

        if len(candidates) >= MAX_CANDIDATES:
            break

    ranked = []
    histories = {}

    for item in candidates:
        symbol = item["symbol"]

        try:
            candles = fetch_candles(
                symbol,
                60,
                HISTORY_1M
            )

            candles = valid_market_candles(candles)

            if len(candles) < MIN_CANDLES_1M:
                logger.info(
                    "Market skipped: %s | insufficient candles",
                    symbol
                )
                continue

            payout = parse_number(item.get("payout"))

            score = market_score(candles, payout)

            if score <= 0:
                continue

            ranked.append({
                "symbol": symbol,
                "score": score,
                "payout": payout
            })

            histories[symbol] = candles[-MAX_CANDLES:]

        except Exception:
            logger.exception(
                "Market evaluation failed: %s",
                symbol
            )

    ranked.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    selected = ranked[:MAX_SYMBOLS]

    return selected, histories


def stream_worker(symbols, tick_queue, stop_event):
    while not stop_event.is_set():
        try:
            logger.info(
                "Connecting to OTC live stream: %s",
                symbols
            )

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

    original_price = parse_number(
        original.get("price"), price
    )

    if original_price <= 0:
        return False, "سعر الإشارة غير صالح"

    movement = abs(price - original_price) / original_price

    if movement > 0.002:
        return False, "تحرك السعر بعيداً عن نقطة الإشارة"

    return True, "تم تأكيد الإشارة"


async def scanner_loop(send_signal, send_result):
    logger.info("OTC Smart Analyzer starting")
    logger.info("Analysis timeframe: 1m only")
    logger.info("Dynamic market selection enabled")
    logger.info("Trade history recording enabled")

    logger.info(
        "Market refresh interval: %s seconds",
        MARKET_REFRESH_SECONDS
    )

    selected, initial_histories = await asyncio.to_thread(
        rank_otc_markets,
        []
    )

    if not selected:
        logger.error("No suitable OTC markets found")
        return

    symbols = [item["symbol"] for item in selected]

    logger.info(
        "Selected OTC markets: %s",
        [
            (item["symbol"], item["score"])
            for item in selected
        ]
    )

    candles_1m = {
        symbol: initial_histories.get(symbol, [])
        for symbol in symbols
    }

    current_1m = {}

    for symbol in symbols:
        logger.info(
            "History loaded: %s | 1m=%s",
            symbol,
            len(candles_1m[symbol])
        )

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

    pending_selection = None
    next_refresh = time.monotonic() + MARKET_REFRESH_SECONDS

    async def cancel_alert(symbol, alert, reason):
        message = dict(alert["signal"])
        message["status"] = "CANCELLED"
        message["reason"] = reason

        record_update(
            alert.get("history_id"),
            "CANCELLED",
            {"reason": reason}
        )

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

    async def apply_selection(new_selection, new_histories):
        nonlocal symbols, tick_queue, stop_event, worker
        nonlocal candles_1m, current_1m

        new_symbols = [
            item["symbol"] for item in new_selection
        ]

        if not new_symbols:
            return False

        if new_symbols == symbols:
            logger.info(
                "Market selection unchanged: %s",
                symbols
            )
            return True

        if pending_alerts or pending_trades:
            return False

        logger.info(
            "Changing OTC markets: %s -> %s",
            symbols,
            new_symbols
        )

        stop_event.set()

        await asyncio.to_thread(worker.join, 8)

        if worker.is_alive():
            logger.warning(
                "Old stream has not stopped yet; "
                "market rotation postponed"
            )
            return False

        symbols = new_symbols

        candles_1m = {
            symbol: new_histories.get(symbol, [])
            for symbol in symbols
        }

        current_1m = {}
        tick_queue = queue.Queue()
        stop_event = threading.Event()

        worker = threading.Thread(
            target=stream_worker,
            args=(symbols, tick_queue, stop_event),
            daemon=True
        )

        worker.start()

        logger.info(
            "New OTC markets active: %s",
            [
                (item["symbol"], item["score"])
                for item in new_selection
            ]
        )

        return True

    try:
        while True:
            if time.monotonic() >= next_refresh:
                next_refresh = (
                    time.monotonic() + MARKET_REFRESH_SECONDS
                )

                try:
                    new_selection, new_histories = (
                        await asyncio.to_thread(
                            rank_otc_markets,
                            symbols
                        )
                    )

                    if new_selection:
                        if pending_alerts or pending_trades:
                            pending_selection = (
                                new_selection,
                                new_histories
                            )

                            logger.info(
                                "Market rotation postponed: "
                                "active alerts or trades"
                            )
                        else:
                            applied = await apply_selection(
                                new_selection,
                                new_histories
                            )

                            if not applied:
                                pending_selection = (
                                    new_selection,
                                    new_histories
                                )

                    else:
                        logger.warning(
                            "Market refresh returned no suitable markets"
                        )

                except Exception:
                    logger.exception(
                        "Dynamic market refresh failed"
                    )

            if (
                pending_selection
                and not pending_alerts
                and not pending_trades
            ):
                new_selection, new_histories = pending_selection

                applied = await apply_selection(
                    new_selection,
                    new_histories
                )

                if applied:
                    pending_selection = None

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

                price = parse_number(tick.get("price"))
                timestamp = parse_number(tick.get("time"))

                if price <= 0 or timestamp <= 0:
                    continue

                if symbol not in candles_1m:
                    continue

                minute_bucket = int(timestamp // 60) * 60

                # تسوية الصفقات التجريبية.
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

                    # حفظ النتيجة قبل إرسال رسالة Telegram.
                    record_result(
                        trade.get("history_id"),
                        result,
                        entry_price=trade["entry_price"],
                        exit_price=exit_price,
                        details={
                            "symbol": symbol,
                            "direction": trade["direction"],
                            "strategies": trade.get("strategies", []),
                            "strategy_reasons": trade.get(
                                "strategy_reasons", []
                            ),
                            "entry_time": trade["entry_time"],
                            "exit_time": timestamp
                        }
                    )

                    await send_result(result_data)

                    logger.info(
                        "PAPER RESULT: %s | %s | %s | Strategies=%s",
                        symbol,
                        trade["direction"],
                        result,
                        trade.get("strategies", [])
                    )

                    pending_trades.pop(symbol, None)

                # بناء شموع الدقيقة.
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

                # إعادة التحقق من الإشارة قبل الدخول.
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
                                record_update(
                                    alert.get("history_id"),
                                    "ENTRY_NOT_DELIVERED"
                                )

                                pending_alerts.pop(symbol, None)

                                logger.warning(
                                    "ENTRY CONFIRMATION NOT DELIVERED; "
                                    "PAPER TRADE BLOCKED: %s",
                                    symbol
                                )
                                continue

                            alert["status"] = "CONFIRMED"

                            record_update(
                                alert.get("history_id"),
                                "CONFIRMED",
                                {
                                    "confirmation": reason,
                                    "confirmation_price": price
                                }
                            )

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
                            ),
                            "history_id": alert.get("history_id")
                        }

                        record_update(
                            alert.get("history_id"),
                            "PAPER_TRADE_STARTED",
                            {
                                "entry_price": price,
                                "entry_time": timestamp,
                                "settles_at": timestamp + TRADE_DURATION
                            }
                        )

                        logger.info(
                            "PAPER TRADE STARTED: %s | %s | Strategies=%s",
                            symbol,
                            signal["direction"],
                            signal.get("matching_strategies", [])
                        )

                        pending_alerts.pop(symbol, None)
                        continue

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
                    "status": "WAIT_CONFIRM",
                    "history_id": None
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
                    logger.warning(
                        "PRE-ALERT NOT DELIVERED; "
                        "SIGNAL DISCARDED: %s",
                        symbol
                    )
                    continue

                # تسجيل الإشارة بعد نجاح إرسال التنبيه.
                history_id = record_signal(signal)
                alert["history_id"] = history_id

                if history_id:
                    record_update(
                        history_id,
                        "PRE_ALERT_DELIVERED",
                        {
                            "entry_at": entry_at,
                            "confirm_at": confirm_at
                        }
                    )

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
        await asyncio.to_thread(worker.join, 5)
        logger.info("OTC scanner stopped")
