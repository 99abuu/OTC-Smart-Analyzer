
import asyncio
import logging
import json
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from config import TELEGRAM_BOT_TOKEN
from market_data import fetch_candles, fetch_otc_symbols
from signal_engine import analyze_signal
from scanner import scanner_loop


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

HISTORY_FILE = Path(__file__).with_name("trade_history.jsonl")

MAX_SYMBOLS = 5
MARKETS_PER_PAGE = 12

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["▶️ START", "⏹ STOP"],
        ["📊 STATUS", "📈 STATS"],
        ["🔍 ANALYZE", "🌍 MARKETS"]
    ],
    resize_keyboard=True,
    is_persistent=True
)


# --------------------------------------------------
# FORMATTING
# --------------------------------------------------

def format_strategies(signal):
    strategies = signal.get("matching_strategies", [])
    reasons = signal.get("strategy_reasons", [])

    if not strategies:
        return "لم يتم تحديد اسم الاستراتيجية."

    lines = []

    for index, name in enumerate(strategies):
        reason = (
            reasons[index]
            if index < len(reasons)
            else "تحققت شروط الاستراتيجية"
        )

        lines.append(f"• {name}\n  السبب: {reason}")

    return "\n".join(lines)


def format_signal_details(signal):
    strategies = signal.get("matching_strategies", [])

    return (
        f"📚 الاستراتيجيات المتحققة ({len(strategies)}):\n"
        f"{format_strategies(signal)}"
    )


def format_ai_shadow(signal):
    ai = signal.get("ai_shadow")

    if not isinstance(ai, dict):
        return (
            "🧠 تقييم الذكاء الاصطناعي (Shadow)\n"
            "لم يتوفر تقييم لهذه الإشارة."
        )

    predictions = ai.get("predictions", {})

    if not isinstance(predictions, dict):
        predictions = {}

    status_labels = {
        "READY": "🟢 النموذج جاهز للتوقع",
        "INSUFFICIENT_DATA": "🟡 البيانات غير كافية للتدريب",
        "CLASS_IMBALANCE": "🟡 توازن النتائج غير كافٍ للتدريب"
    }

    lines = [
        "🧠 تقييم الذكاء الاصطناعي (Shadow)",
        f"النموذج: {ai.get('model', 'غير محدد')}"
    ]

    for horizon in (60, 120):
        prediction = predictions.get(str(horizon), {})

        if not isinstance(prediction, dict):
            prediction = {}

        status = str(
            prediction.get("status", "UNKNOWN")
        ).upper()

        status_text = status_labels.get(
            status,
            f"الحالة: {status}"
        )

        samples = prediction.get("samples", 0)
        wins = prediction.get("wins", 0)
        losses = prediction.get("losses", 0)
        probability = prediction.get("win_probability")

        lines.extend([
            "",
            f"⏱️ أفق {horizon} ثانية:",
            status_text,
            f"📊 العينات: {samples}",
            f"🟢 WIN: {wins} | 🔴 LOSS: {losses}"
        ])

        if probability is not None:
            try:
                probability = float(probability)

                if 0 <= probability <= 100:
                    lines.append(
                        f"🎯 احتمال الفوز التقديري للنموذج: "
                        f"{probability:.2f}%"
                    )
                else:
                    lines.append(
                        "🎯 الاحتمال: قيمة غير صالحة"
                    )

            except (TypeError, ValueError):
                lines.append(
                    "🎯 الاحتمال: غير متوفر"
                )

        else:
            lines.append(
                "🎯 لا يوجد توقع رقمي بعد"
            )

    if ai.get("error"):
        lines.append(
            f"\n⚠️ ملاحظة: {ai['error']}"
        )

    lines.extend([
        "",
        "ℹ️ هذا تقييم تجريبي للنموذج وليس ضماناً للنتيجة."
    ])

    return "\n".join(lines)


def format_duration(signal):
    duration = signal.get(
        "selected_duration_seconds",
        signal.get(
            "duration_seconds",
            signal.get("selected_duration", 60)
        )
    )

    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 60

    if duration == 120:
        label = "دقيقتان (120 ثانية)"
    else:
        duration = 60
        label = "دقيقة واحدة (60 ثانية)"

    reason = signal.get(
        "duration_reason",
        "اختيار تجريبي حسب خصائص الإشارة"
    )

    return label, reason


def signal_phase(signal):
    return signal.get("phase") or signal.get("status")


def format_strategy_performance(signal):
    strategies = signal.get("matching_strategies", [])

    if isinstance(strategies, str):
        strategies = [strategies]

    strategies = list(dict.fromkeys(strategies))

    if not strategies:
        return "غير محددة"

    try:
        signals, results, _, _ = read_trade_history()
    except Exception:
        logger.exception(
            "Could not read history for strategy performance"
        )
        return "\n".join(
            f"• {strategy}: غير متوفرة بعد"
            for strategy in strategies
        )

    strategy_stats = defaultdict(Counter)

    for signal_id, result in results.items():
        outcome = str(
            result.get("outcome", "")
        ).upper()

        if outcome not in ("WIN", "LOSS", "DRAW"):
            continue

        historical_signal = signals.get(signal_id, {})
        historical_strategies = historical_signal.get(
            "matching_strategies", []
        )

        if isinstance(historical_strategies, str):
            historical_strategies = [historical_strategies]

        if not isinstance(historical_strategies, list):
            continue

        for strategy in set(historical_strategies):
            strategy_stats[strategy][outcome] += 1

    lines = []

    for strategy in strategies:
        stats = strategy_stats[strategy]

        wins = stats["WIN"]
        losses = stats["LOSS"]
        decisive = wins + losses

        if decisive > 0:
            rate = wins / decisive * 100
            rate_text = f"{rate:.1f}%"
            sample_text = f"{decisive} صفقة حاسمة"
        else:
            rate_text = "غير متوفرة بعد"
            sample_text = "لا توجد نتائج حاسمة"

        lines.append(
            f"• {strategy}: {rate_text} ({sample_text})"
        )

    return "\n".join(lines)


def format_compact_signal(signal, phase):
    duration_label, _ = format_duration(signal)

    symbol = signal.get("symbol", "غير محدد")
    direction = signal.get("direction", "غير محدد")
    entry_time = signal.get("entry_time", "غير محدد")

    strategy_text = format_strategy_performance(signal)

    if phase == "PRE_ALERT":
        heading = "🔔 فرصة OTC قادمة"
    else:
        heading = "✅ تأكيد الإشارة التجريبية"

    return (
        f"{heading}\n\n"
        f"📊 الأداة: {symbol}\n"
        f"📈 الاتجاه: {direction}\n"
        f"🕒 موعد الدخول: {entry_time}\n"
        f"⏱️ مدة الصفقة: {duration_label}\n\n"
        f"📚 الاستراتيجية ونسبة نجاحها التاريخية:\n"
        f"{strategy_text}\n\n"
        "⚠️ جميع الإشارات والنتائج تجريبية."
    )


# --------------------------------------------------
# TRADE HISTORY
# --------------------------------------------------

def read_trade_history():
    signals = {}
    results = {}
    updates = defaultdict(set)
    horizon_results = defaultdict(dict)

    if not HISTORY_FILE.exists():
        return signals, results, updates, horizon_results

    with HISTORY_FILE.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                event = json.loads(line)
                event_type = event.get("event")
                signal_id = event.get("signal_id")

                if not signal_id:
                    continue

                if event_type == "signal":
                    signals[signal_id] = event.get("signal", {})

                elif event_type == "update":
                    status = str(
                        event.get("status", "")
                    ).upper()

                    updates[signal_id].add(status)

                elif event_type == "result":
                    results[signal_id] = event

                elif event_type == "horizon_result":
                    try:
                        horizon = int(
                            event.get("horizon_seconds")
                        )
                    except (TypeError, ValueError):
                        continue

                    outcome = str(
                        event.get("outcome", "")
                    ).upper()

                    if horizon not in (60, 120):
                        continue

                    if outcome not in ("WIN", "LOSS", "DRAW"):
                        continue

                    horizon_results[signal_id][horizon] = event

            except (json.JSONDecodeError, TypeError):
                continue

    return signals, results, updates, horizon_results


def build_horizon_stats(horizon_results):
    stats = {
        60: Counter(),
        120: Counter()
    }

    paired = {
        "60_better": 0,
        "120_better": 0,
        "same": 0
    }

    outcome_score = {
        "LOSS": 0,
        "DRAW": 1,
        "WIN": 2
    }

    for signal_id, horizons in horizon_results.items():
        for duration in (60, 120):
            event = horizons.get(duration)

            if event:
                outcome = str(
                    event.get("outcome", "")
                ).upper()

                if outcome in ("WIN", "LOSS", "DRAW"):
                    stats[duration][outcome] += 1

        result_60 = horizons.get(60)
        result_120 = horizons.get(120)

        if result_60 and result_120:
            outcome_60 = str(
                result_60.get("outcome", "")
            ).upper()

            outcome_120 = str(
                result_120.get("outcome", "")
            ).upper()

            if (
                outcome_60 not in outcome_score
                or outcome_120 not in outcome_score
            ):
                continue

            if outcome_score[outcome_60] > outcome_score[outcome_120]:
                paired["60_better"] += 1

            elif outcome_score[outcome_120] > outcome_score[outcome_60]:
                paired["120_better"] += 1

            else:
                paired["same"] += 1

    return stats, paired


def format_horizon_line(duration, stats):
    wins = stats["WIN"]
    losses = stats["LOSS"]
    draws = stats["DRAW"]

    completed = wins + losses + draws
    decisive = wins + losses

    rate = wins / decisive * 100 if decisive else 0

    return (
        f"\n⏱️ مدة {duration} ثانية:\n"
        f"  🟢 {wins} WIN | 🔴 {losses} LOSS | "
        f"🟡 {draws} DRAW\n"
        f"  🎯 نسبة الفوز: {rate:.1f}%\n"
        f"  📊 المكتملة: {completed}"
    )


def build_stats_message(data):
    signals, results, updates, horizon_results = data

    valid_results = {}

    for signal_id, result in results.items():
        outcome = str(
            result.get("outcome", "")
        ).upper()

        if outcome in ("WIN", "LOSS", "DRAW"):
            valid_results[signal_id] = result

    outcomes = Counter(
        str(result["outcome"]).upper()
        for result in valid_results.values()
    )

    wins = outcomes["WIN"]
    losses = outcomes["LOSS"]
    draws = outcomes["DRAW"]

    completed = wins + losses + draws
    decisive = wins + losses

    win_rate = wins / decisive * 100 if decisive else 0

    cancelled_ids = {
        signal_id
        for signal_id, statuses in updates.items()
        if any("CANCEL" in status for status in statuses)
    }

    lines = [
        "📊 إحصائيات OTC Smart Analyzer",
        "",
        f"📨 الإشارات المسجلة: {len(signals)}",
        f"📈 الصفقات المكتملة: {completed}",
        "",
        f"🟢 WIN: {wins}",
        f"🔴 LOSS: {losses}",
        f"🟡 DRAW: {draws}",
        f"🎯 نسبة الفوز: {win_rate:.1f}%",
        f"❌ الإشارات الملغاة: {len(cancelled_ids)}",
        "",
        "━━━━━━━━━━━━━━━━",
        "⏱️ مقارنة مدة الصفقة"
    ]

    horizon_stats, paired = build_horizon_stats(
        horizon_results
    )

    lines.append(
        format_horizon_line(60, horizon_stats[60])
    )

    lines.append(
        format_horizon_line(120, horizon_stats[120])
    )

    paired_count = (
        paired["60_better"]
        + paired["120_better"]
        + paired["same"]
    )

    lines.extend([
        "",
        "📊 مقارنة الإشارات المشتركة على المدتين:",
        f"  عدد الإشارات المكتملة على المدتين: {paired_count}",
        f"  🟢 نتيجة 60 ثانية أفضل: {paired['60_better']}",
        f"  🔵 نتيجة 120 ثانية أفضل: {paired['120_better']}",
        f"  🟡 نفس النتيجة: {paired['same']}",
        "",
        "━━━━━━━━━━━━━━━━",
        "📚 أداء الاستراتيجيات"
    ])

    strategy_stats = defaultdict(Counter)

    for signal_id, result in valid_results.items():
        signal = signals.get(signal_id, {})
        strategies = signal.get("matching_strategies", [])

        if isinstance(strategies, str):
            strategies = [strategies]

        for strategy in set(strategies):
            strategy_stats[strategy][result["outcome"]] += 1

    if strategy_stats:
        for strategy, stats in sorted(
            strategy_stats.items(),
            key=lambda item: (
                item[1]["WIN"] /
                max(1, item[1]["WIN"] + item[1]["LOSS"]),
                item[1]["WIN"] + item[1]["LOSS"]
            ),
            reverse=True
        ):
            total = (
                stats["WIN"]
                + stats["LOSS"]
                + stats["DRAW"]
            )

            decisive = stats["WIN"] + stats["LOSS"]
            rate = stats["WIN"] / decisive * 100 if decisive else 0

            lines.append(
                f"\n• {strategy}\n"
                f"  {stats['WIN']}W / {stats['LOSS']}L / "
                f"{stats['DRAW']}D | الفوز {rate:.1f}% | "
                f"الإجمالي {total}"
            )
    else:
        lines.append(
            "\nلا توجد بيانات استراتيجية مكتملة بعد."
        )

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━",
        "🌍 أداء الأسواق"
    ])

    market_stats = defaultdict(Counter)

    for signal_id, result in valid_results.items():
        signal = signals.get(signal_id, {})
        symbol = signal.get("symbol", "غير معروف")

        market_stats[symbol][result["outcome"]] += 1

    if market_stats:
        for symbol, stats in sorted(
            market_stats.items(),
            key=lambda item: (
                item[1]["WIN"] + item[1]["LOSS"]
            ),
            reverse=True
        ):
            decisive = stats["WIN"] + stats["LOSS"]
            rate = stats["WIN"] / decisive * 100 if decisive else 0

            lines.append(
                f"\n• {symbol}: "
                f"{stats['WIN']}W / {stats['LOSS']}L / "
                f"{stats['DRAW']}D | {rate:.1f}%"
            )
    else:
        lines.append(
            "\nلا توجد نتائج أسواق محفوظة بعد."
        )

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━",
        "⚠️ الإحصائيات مبنية على سجل Paper Trading المحلي.",
        "مقارنة المدتين تعتمد على الإشارات التي حُفظت نتائجها.",
        "قد لا تشمل النتائج السابقة التي لم تُحفظ في هذا الملف.",
        "وهي ليست نتائج تسوية Pocket Option الفعلية."
    ])

    message = "\n".join(lines)

    if len(message) > 4000:
        message = message[:3950] + "\n\n... تم اختصار التقرير."

    return message


# --------------------------------------------------
# EXPORT TRADE HISTORY
# --------------------------------------------------

async def export_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_controller(update, context):
        await update.effective_message.reply_text(
            "❌ هذا الأمر متاح لحساب التحكم فقط.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    if not HISTORY_FILE.exists():
        await update.effective_message.reply_text(
            "❌ لم يتم العثور على ملف سجل الصفقات.\n\n"
            "المسار المتوقع:\n"
            f"{HISTORY_FILE}\n\n"
            "تأكد أن البوت سجّل إشارات على هذا الخادم.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    try:
        file_size = HISTORY_FILE.stat().st_size

        if file_size == 0:
            await update.effective_message.reply_text(
                "⚠️ ملف سجل الصفقات موجود لكنه فارغ.",
                reply_markup=MAIN_KEYBOARD
            )
            return

        await update.effective_message.reply_text(
            "📦 جاري تجهيز سجل الصفقات وإرساله..."
        )

        with HISTORY_FILE.open("rb") as history_file:
            await update.effective_message.reply_document(
                document=history_file,
                filename="trade_history.jsonl",
                caption=(
                    "📊 سجل OTC Smart Analyzer\n"
                    f"حجم الملف: {file_size / 1024:.1f} KB\n"
                    "هذا هو السجل الخام للإشارات والنتائج."
                ),
                reply_markup=MAIN_KEYBOARD
            )

        logger.info(
            "Trade history exported to controller user_id=%s",
            update.effective_user.id
        )

    except Exception:
        logger.exception("Trade history export failed")

        await update.effective_message.reply_text(
            "❌ تعذر إرسال ملف سجل الصفقات.\n"
            "راجع سجلات تشغيل البوت.",
            reply_markup=MAIN_KEYBOARD
        )


# --------------------------------------------------
# CONTROLLER AND SCANNER MANAGEMENT
# --------------------------------------------------

def is_controller(update, context):
    controller_id = context.application.bot_data.get(
        "controller_user_id"
    )

    user = update.effective_user

    return (
        user is not None
        and controller_id is not None
        and user.id == controller_id
    )


async def send_controller_status(app, message):
    controller_id = app.bot_data.get(
        "controller_chat_id"
    )

    if not controller_id:
        return

    try:
        await app.bot.send_message(
            chat_id=controller_id,
            text=message,
            reply_markup=MAIN_KEYBOARD
        )

    except Exception:
        logger.exception(
            "Controller status delivery failed"
        )


async def start_scanner_task(app):
    task = app.bot_data.get("scanner_task")

    if task is not None and not task.done():
        return task

    selection_queue = app.bot_data.get(
        "selection_queue"
    )

    if selection_queue is None:
        selection_queue = asyncio.Queue()
        app.bot_data["selection_queue"] = selection_queue

    async def notify(signal):
        phase = signal_phase(signal)

        if phase == "PRE_ALERT":
            message = format_compact_signal(
                signal,
                "PRE_ALERT"
            )

            await send_to_subscribers(
                app,
                message
            )

            # AI Shadow رسالة مستقلة عن رسالة الإشارة
            ai_text = format_ai_shadow(signal)

            await send_to_subscribers(
                app,
                ai_text
            )

            return True

        elif phase == "ENTRY":
            message = format_compact_signal(
                signal,
                "ENTRY"
            )

            return await send_to_subscribers(
                app,
                message
            )

        elif phase == "CANCELLED":
            strategies_text = format_signal_details(signal)

            entry_time = (
                signal.get("entry_time")
                or "غير محدد"
            )

            reason = (
                signal.get("cancel_reason")
                or signal.get("reason")
                or "تغيرت شروط الإشارة"
            )

            message = (
                "❌ تم إلغاء فرصة OTC\n\n"
                f"الأداة: {signal.get('symbol', 'غير محدد')}\n"
                f"الاتجاه السابق: {signal.get('direction', 'غير محدد')}\n"
                f"موعد الدخول: {entry_time}\n"
                f"السبب: {reason}\n\n"
                f"{strategies_text}\n\n"
                "لم يتم تسجيل صفقة لهذه الفرصة."
            )

            return await send_to_subscribers(
                app,
                message
            )

        else:
            logger.error(
                "Unknown signal phase/status: %s",
                phase
            )
            return False

    async def notify_result(result):
        labels = {
            "WIN": "🟢 WIN — نتيجة افتراضية",
            "LOSS": "🔴 LOSS — نتيجة افتراضية",
            "DRAW": "🟡 DRAW — تعادل"
        }

        strategies = result.get("strategies", [])

        strategy_text = (
            "\n".join(f"• {s}" for s in strategies)
            if strategies
            else "غير متوفرة"
        )

        _, duration_reason = format_duration(result)

        duration = result.get(
            "duration_seconds",
            result.get("selected_duration_seconds", 60)
        )

        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 60

        duration_label = (
            "دقيقتان (120 ثانية)"
            if duration == 120
            else "دقيقة واحدة (60 ثانية)"
        )

        message = (
            "📊 نتيجة الصفقة التجريبية\n\n"
            f"{labels.get(result['result'], result['result'])}\n\n"
            f"الأداة: {result['symbol']}\n"
            f"الاتجاه: {result['direction']}\n"
            f"مدة قياس النتيجة: {duration_label}\n"
            f"سبب اختيار المدة الأساسية: {duration_reason}\n"
            f"سعر البداية: {result['entry_price']}\n"
            f"سعر النهاية: {result['exit_price']}\n"
            f"وقت البداية: {result['entry_time']}\n"
            f"وقت النهاية: {result['exit_time']}\n\n"
            "الاستراتيجيات:\n"
            f"{strategy_text}\n\n"
            "⚠️ النتيجة محسوبة من أسعار OTCharts، "
            "وليست تسوية Pocket Option الفعلية."
        )

        return await send_to_subscribers(
            app,
            message
        )

    async def notify_status(message):
        await send_controller_status(
            app,
            str(message)
        )

    app.bot_data["scanner_task"] = asyncio.create_task(
        scanner_loop(
            notify,
            notify_result,
            selection_queue,
            notify_status
        )
    )

    logger.info(
        "Scanner task created and waiting for market selection"
    )

    return app.bot_data["scanner_task"]


async def queue_selection(app, selection):
    selection_queue = app.bot_data.get(
        "selection_queue"
    )

    if selection_queue is None:
        selection_queue = asyncio.Queue()
        app.bot_data["selection_queue"] = selection_queue

    await start_scanner_task(app)
    await selection_queue.put(selection)


# --------------------------------------------------
# MARKET PICKER
# --------------------------------------------------

async def fetch_open_markets():
    symbols = await asyncio.to_thread(
        fetch_otc_symbols
    )

    if not isinstance(symbols, list):
        return []

    markets = []

    for item in symbols:
        if not isinstance(item, dict):
            continue

        if item.get("isOtc") is not True:
            continue

        if item.get("open") is not True:
            continue

        symbol = item.get("symbol")

        if not symbol:
            continue

        markets.append({
            "symbol": str(symbol),
            "payout": item.get("payout", 0)
        })

    markets.sort(
        key=lambda item: item["symbol"]
    )

    return markets


def build_market_keyboard(markets, selected, page):
    total_pages = max(
        1,
        (len(markets) + MARKETS_PER_PAGE - 1)
        // MARKETS_PER_PAGE
    )

    page = max(
        0,
        min(page, total_pages - 1)
    )

    start_index = page * MARKETS_PER_PAGE

    page_markets = markets[
        start_index:
        start_index + MARKETS_PER_PAGE
    ]

    keyboard = []

    for index in range(0, len(page_markets), 2):
        row = []

        for offset, item in enumerate(
            page_markets[index:index + 2]
        ):
            symbol = item["symbol"]

            marker = (
                "✅ "
                if symbol in selected
                else ""
            )

            market_index = start_index + index + offset

            row.append(
                InlineKeyboardButton(
                    f"{marker}{symbol}",
                    callback_data=f"mkt:toggle:{market_index}"
                )
            )

        keyboard.append(row)

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ السابق",
                callback_data=f"mkt:page:{page - 1}"
            )
        )

    navigation.append(
        InlineKeyboardButton(
            f"{page + 1}/{total_pages}",
            callback_data="mkt:noop"
        )
    )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "التالي ➡️",
                callback_data=f"mkt:page:{page + 1}"
            )
        )

    keyboard.append(navigation)

    keyboard.append([
        InlineKeyboardButton(
            "🔄 تحديث الأسواق",
            callback_data="mkt:refresh"
        ),
        InlineKeyboardButton(
            "🗑 مسح الاختيار",
            callback_data="mkt:clear"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            f"✅ تأكيد الاختيار ({len(selected)}/{MAX_SYMBOLS})",
            callback_data="mkt:confirm"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "⚡ اختيار تلقائي لأفضل 5",
            callback_data="mkt:auto"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "❌ إلغاء",
            callback_data="mkt:cancel"
        )
    ])

    return InlineKeyboardMarkup(keyboard)


async def show_market_picker(
    update,
    context,
    refresh=False
):
    if not is_controller(update, context):
        if update.callback_query:
            await update.callback_query.answer(
                "ليس لديك صلاحية التحكم.",
                show_alert=True
            )
        return

    try:
        if refresh or "available_markets" not in context.user_data:
            markets = await fetch_open_markets()
            context.user_data["available_markets"] = markets

        else:
            markets = context.user_data["available_markets"]

        selected = context.user_data.setdefault(
            "selected_markets",
            []
        )

        page = context.user_data.get(
            "market_page",
            0
        )

        if not markets:
            text = (
                "❌ لم يتم العثور على أسواق OTC مفتوحة حالياً."
            )

            if update.callback_query:
                await update.callback_query.edit_message_text(text)
            else:
                await update.effective_message.reply_text(
                    text,
                    reply_markup=MAIN_KEYBOARD
                )

            return

        total_pages = max(
            1,
            (len(markets) + MARKETS_PER_PAGE - 1)
            // MARKETS_PER_PAGE
        )

        page = max(
            0,
            min(page, total_pages - 1)
        )

        context.user_data["market_page"] = page

        text = (
            "🌍 اختيار أسواق OTC\n\n"
            "اختر سوقاً واحداً أو أكثر.\n"
            f"الحد الأقصى: {MAX_SYMBOLS} أسواق.\n\n"
            f"الأسواق المفتوحة: {len(markets)}\n"
            f"الأسواق المختارة: {len(selected)}/{MAX_SYMBOLS}\n\n"
            "اضغط على اسم السوق لتحديده أو إلغاء تحديده، "
            "ثم اضغط تأكيد الاختيار."
        )

        keyboard = build_market_keyboard(
            markets,
            selected,
            page
        )

        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=keyboard
            )

        else:
            await update.effective_message.reply_text(
                text,
                reply_markup=keyboard
            )

    except Exception:
        logger.exception("Market picker failed")

        if update.callback_query:
            await update.callback_query.answer(
                "تعذر تحميل الأسواق.",
                show_alert=True
            )
        else:
            await update.effective_message.reply_text(
                "❌ تعذر تحميل الأسواق. راجع سجلات التشغيل."
            )


# --------------------------------------------------
# MARKET CALLBACKS
# --------------------------------------------------

async def market_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not is_controller(update, context):
        await query.answer(
            "ليس لديك صلاحية التحكم بالبوت.",
            show_alert=True
        )
        return

    await query.answer()

    data = query.data or ""

    if data == "mkt:noop":
        return

    if data == "mkt:cancel":
        await query.edit_message_text(
            "تم إلغاء قائمة اختيار الأسواق."
        )
        return

    if data == "mkt:refresh":
        await show_market_picker(
            update,
            context,
            refresh=True
        )
        return

    if data == "mkt:clear":
        context.user_data["selected_markets"] = []

        await show_market_picker(
            update,
            context
        )
        return

    if data.startswith("mkt:page:"):
        try:
            page = int(data.split(":")[-1])
        except ValueError:
            return

        context.user_data["market_page"] = page

        await show_market_picker(
            update,
            context
        )
        return

    if data.startswith("mkt:toggle:"):
        try:
            index = int(data.split(":")[-1])
        except ValueError:
            return

        markets = context.user_data.get(
            "available_markets",
            []
        )

        if index < 0 or index >= len(markets):
            await query.answer(
                "هذا السوق لم يعد موجوداً في القائمة.",
                show_alert=True
            )
            return

        symbol = markets[index]["symbol"]

        selected = context.user_data.setdefault(
            "selected_markets",
            []
        )

        if symbol in selected:
            selected.remove(symbol)

        else:
            if len(selected) >= MAX_SYMBOLS:
                await query.answer(
                    f"الحد الأقصى هو {MAX_SYMBOLS} أسواق.",
                    show_alert=True
                )
                return

            selected.append(symbol)

        await show_market_picker(
            update,
            context
        )
        return

    if data == "mkt:confirm":
        selected = context.user_data.get(
            "selected_markets",
            []
        )

        if not selected:
            await query.answer(
                "اختر سوقاً واحداً على الأقل.",
                show_alert=True
            )
            return

        if len(selected) > MAX_SYMBOLS:
            await query.answer(
                f"الحد الأقصى {MAX_SYMBOLS} أسواق.",
                show_alert=True
            )
            return

        selection = {
            "mode": "manual",
            "symbols": list(selected)
        }

        await queue_selection(
            context.application,
            selection
        )

        context.application.bot_data[
            "last_selection"
        ] = selection

        await query.edit_message_text(
            "✅ تم إرسال اختيار الأسواق إلى الماسح.\n\n"
            "الأسواق المطلوبة:\n"
            + "\n".join(f"• {symbol}" for symbol in selected)
            + "\n\nسيتم التحقق من توفر البيانات قبل بدء المراقبة."
        )

        return

    if data == "mkt:auto":
        selection = {
            "mode": "auto"
        }

        await queue_selection(
            context.application,
            selection
        )

        context.application.bot_data[
            "last_selection"
        ] = selection

        await query.edit_message_text(
            "⚡ تم تفعيل الاختيار التلقائي.\n\n"
            "سيقوم الماسح باختيار أفضل 5 أسواق OTC "
            "وفق آلية التقييم والتدوير الحالية."
        )

        return


# --------------------------------------------------
# COMMANDS
# --------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat_id = update.effective_chat.id
    user = update.effective_user
    app = context.application

    subscribers = app.bot_data.setdefault(
        "subscribers",
        set()
    )

    subscribers.add(chat_id)

    controller_id = app.bot_data.get(
        "controller_user_id"
    )

    if controller_id is None:
        app.bot_data["controller_user_id"] = user.id
        app.bot_data["controller_chat_id"] = chat_id

        is_new_controller = True

        logger.info(
            "Controller registered: user_id=%s",
            user.id
        )

    else:
        is_new_controller = (
            user.id == controller_id
        )

    message = (
        "🤖 OTC Smart Analyzer\n\n"
        "✅ تم تسجيلك لاستقبال الإشارات تلقائياً.\n"
        "📊 نتائج Paper Trading وإحصائيات الأداء.\n"
        "⏱️ مقارنة نتائج 60 و120 ثانية.\n"
        "🧠 الاستراتيجيات مستقلة ومتعددة.\n"
        "📈 التحليل على فريم الدقيقة فقط.\n\n"
        "/status - حالة البوت\n"
        "/markets - اختيار الأسواق\n"
        "/analyze - تحليل يدوي\n"
        "/testsymbols - قائمة الأدوات\n"
        "/stats - إحصائيات الأداء\n"
        "/exporthistory - استخراج سجل الصفقات\n\n"
    )

    if is_new_controller:
        message += (
            "👑 أنت حساب التحكم الأول.\n"
            "استخدم START لاختيار الأسواق وبدء المراقبة.\n"
            "يمكنك أيضاً استخدام STOP لإيقاف المراقبة."
        )

    elif is_controller(update, context):
        message += (
            "👑 لديك صلاحية التحكم بالأسواق والماسح."
        )

    else:
        message += (
            "👤 أنت مشترك لاستقبال الإشارات والنتائج.\n"
            "التحكم بالأسواق والماسح متاح لحساب التحكم فقط."
        )

    await update.message.reply_text(
        message,
        reply_markup=MAIN_KEYBOARD
    )

    if is_controller(update, context):
        await show_market_picker(
            update,
            context
        )

    else:
        await update.message.reply_text(
            "⚠️ جميع النتائج افتراضية وتجريبية فقط.",
            reply_markup=MAIN_KEYBOARD
        )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    app = context.application

    task = app.bot_data.get("scanner_task")

    running = (
        task is not None
        and not task.done()
    )

    last_selection = app.bot_data.get(
        "last_selection"
    )

    if last_selection:
        if last_selection.get("mode") == "manual":
            markets_text = ", ".join(
                last_selection.get("symbols", [])
            )
            mode_text = "يدوي"

        else:
            markets_text = "أفضل 5 أسواق تلقائياً"
            mode_text = "تلقائي"

    else:
        markets_text = "لم يتم الاختيار بعد"
        mode_text = "بانتظار الاختيار"

    subscribers = app.bot_data.get(
        "subscribers",
        set()
    )

    await update.message.reply_text(
        "📊 حالة OTC Smart Analyzer\n\n"
        f"الماسح: {'🟢 يعمل أو ينتظر الاختيار' if running else '🔴 متوقف'}\n"
        f"وضع الأسواق: {mode_text}\n"
        f"الأسواق: {markets_text}\n"
        "الحد الأقصى: 5 أسواق\n"
        "فريم التحليل: دقيقة واحدة فقط\n"
        "مقارنة النتائج: 60 و120 ثانية\n"
        f"المشتركون الحاليون: {len(subscribers)}\n\n"
        "⚠️ جميع النتائج تجريبية وافتراضية.",
        reply_markup=MAIN_KEYBOARD
    )


async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "📊 جاري قراءة سجل الصفقات..."
    )

    try:
        data = await asyncio.to_thread(
            read_trade_history
        )

        message = build_stats_message(data)

        await update.message.reply_text(
            message,
            reply_markup=MAIN_KEYBOARD
        )

    except Exception:
        logger.exception(
            "Statistics generation failed"
        )

        await update.message.reply_text(
            "❌ تعذر قراءة سجل الصفقات. راجع سجلات التشغيل.",
            reply_markup=MAIN_KEYBOARD
        )


async def analyze(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    symbol = (
        context.args[0]
        if context.args
        else None
    )

    await update.message.reply_text(
        "🔍 جاري التحليل على فريم الدقيقة..."
    )

    try:
        symbols = await asyncio.to_thread(
            fetch_otc_symbols
        )

        if symbol:
            item = next(
                (
                    s for s in symbols
                    if s["symbol"].lower() == symbol.lower()
                    and s.get("isOtc") is True
                    and s.get("open") is True
                ),
                None
            )

        else:
            item = next(
                (
                    s for s in symbols
                    if s.get("isOtc") is True
                    and s.get("open") is True
                ),
                None
            )

        if not item:
            await update.message.reply_text(
                "لم يتم العثور على أداة OTC مفتوحة."
            )
            return

        selected = item["symbol"]

        candles_1m = await asyncio.to_thread(
            fetch_candles,
            selected,
            60,
            300
        )

        signal = analyze_signal(
            candles_1m,
            symbol=selected
        )

        if not signal:
            await update.message.reply_text(
                f"📊 {selected}\n"
                "لا توجد استراتيجية أعطت إشارة صالحة حالياً."
            )
            return

        duration_label, duration_reason = format_duration(
            signal
        )

        message = (
            "📡 تحليل يدوي تجريبي\n\n"
            f"الأداة: {selected}\n"
            "الفريم: 1 دقيقة\n"
            f"الاتجاه: {signal['direction']}\n"
            f"السعر المرجعي: {signal.get('price', 'غير متوفر')}\n\n"
            f"⏱️ المدة المختارة تجريبياً: {duration_label}\n"
            f"🧠 سبب اختيار المدة: {duration_reason}\n\n"
            f"{format_signal_details(signal)}\n\n"
            "⚠️ هذا تحليل يدوي فقط، وليس تأكيد دخول. "
            "الإشارات الآلية تخضع لإعادة التحقق قبل الدخول."
        )

        await update.message.reply_text(
            message,
            reply_markup=MAIN_KEYBOARD
        )

    except Exception:
        logger.exception(
            "Manual analysis failed"
        )

        await update.message.reply_text(
            "❌ تعذر التحليل. راجع سجلات التشغيل.",
            reply_markup=MAIN_KEYBOARD
        )


async def testsymbols(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        symbols = await asyncio.to_thread(
            fetch_otc_symbols
        )

        symbols = [
            item for item in symbols
            if isinstance(item, dict)
            and item.get("isOtc") is True
            and item.get("open") is True
        ]

        def payout_value(item):
            try:
                return float(item.get("payout") or 0)
            except (TypeError, ValueError):
                return 0

        symbols.sort(
            key=payout_value,
            reverse=True
        )

        await update.message.reply_text(
            "أفضل 5 أدوات OTC المفتوحة:\n\n"
            + "\n".join(
                f"{item['symbol']} | {item.get('payout')}%"
                for item in symbols[:5]
            ),
            reply_markup=MAIN_KEYBOARD
        )

    except Exception:
        logger.exception(
            "Symbol list failed"
        )

        await update.message.reply_text(
            "❌ تعذر جلب قائمة الأدوات. راجع سجلات التشغيل.",
            reply_markup=MAIN_KEYBOARD
        )


async def markets(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_controller(update, context):
        await update.message.reply_text(
            "ليس لديك صلاحية تغيير الأسواق.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    await show_market_picker(
        update,
        context,
        refresh=True
    )


# --------------------------------------------------
# START / STOP BUTTONS
# --------------------------------------------------

async def stop_scanner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_controller(update, context):
        await update.message.reply_text(
            "❌ ليس لديك صلاحية إيقاف الماسح.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    task = context.application.bot_data.get(
        "scanner_task"
    )

    if task is None or task.done():
        await update.message.reply_text(
            "🔴 الماسح متوقف بالفعل.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    await queue_selection(
        context.application,
        {
            "mode": "stop"
        }
    )

    await update.message.reply_text(
        "⏹ تم إرسال طلب إيقاف الماسح.\n\n"
        "سيعالج الماسح طلب الإيقاف وفق آلية إدارة "
        "الإشارات والصفقات المعلقة.",
        reply_markup=MAIN_KEYBOARD
    )


async def start_scanner_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_controller(update, context):
        await update.message.reply_text(
            "❌ ليس لديك صلاحية تشغيل الماسح.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    task = context.application.bot_data.get(
        "scanner_task"
    )

    if task is not None and not task.done():
        last_selection = context.application.bot_data.get(
            "last_selection"
        )

        if last_selection is not None:
            await update.message.reply_text(
                "🟢 الماسح يعمل بالفعل.\n"
                "يمكنك تغيير الأسواق من زر MARKETS.",
                reply_markup=MAIN_KEYBOARD
            )
        else:
            await show_market_picker(
                update,
                context,
                refresh=True
            )

        return

    last_selection = context.application.bot_data.get(
        "last_selection"
    )

    if last_selection is None:
        await show_market_picker(
            update,
            context,
            refresh=True
        )
        return

    await queue_selection(
        context.application,
        last_selection
    )

    await update.message.reply_text(
        "▶️ تم إرسال طلب تشغيل الماسح باستخدام آخر اختيار محفوظ.",
        reply_markup=MAIN_KEYBOARD
    )


async def text_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = (
        update.message.text.strip()
        if update.message and update.message.text
        else ""
    )

    if text == "▶️ START":
        await start_scanner_button(
            update,
            context
        )

    elif text == "⏹ STOP":
        await stop_scanner(
            update,
            context
        )

    elif text == "📊 STATUS":
        await status(
            update,
            context
        )

    elif text == "📈 STATS":
        await stats(
            update,
            context
        )

    elif text == "🔍 ANALYZE":
        await analyze(
            update,
            context
        )

    elif text == "🌍 MARKETS":
        await markets(
            update,
            context
        )


# --------------------------------------------------
# SUBSCRIBER NOTIFICATIONS
# --------------------------------------------------

async def send_to_subscribers(app, message):
    subscribers = list(
        app.bot_data.get("subscribers", set())
    )

    if not subscribers:
        logger.warning(
            "No Telegram subscribers available"
        )
        return False

    success_count = 0

    for chat_id in subscribers:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=message
            )

            success_count += 1

        except Exception:
            logger.exception(
                "Telegram delivery failed for chat %s",
                chat_id
            )

    return success_count == len(subscribers)


# --------------------------------------------------
# APPLICATION STARTUP
# --------------------------------------------------

async def post_init(app):
    app.bot_data["subscribers"] = set()

    app.bot_data["controller_user_id"] = None
    app.bot_data["controller_chat_id"] = None

    app.bot_data["selection_queue"] = asyncio.Queue()
    app.bot_data["scanner_task"] = None
    app.bot_data["last_selection"] = None

    logger.info(
        "Telegram bot initialized. Waiting for controller."
    )


async def post_shutdown(app):
    task = app.bot_data.get(
        "scanner_task"
    )

    if task and not task.done():
        task.cancel()

        try:
            await task

        except asyncio.CancelledError:
            pass

        except Exception:
            logger.exception(
                "Scanner shutdown failed"
            )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("status", status)
    )

    app.add_handler(
        CommandHandler("analyze", analyze)
    )

    app.add_handler(
        CommandHandler("testsymbols", testsymbols)
    )

    app.add_handler(
        CommandHandler("stats", stats)
    )

    app.add_handler(
        CommandHandler("exporthistory", export_history)
    )

    app.add_handler(
        CommandHandler("markets", markets)
    )

    app.add_handler(
        CommandHandler("stop", stop_scanner)
    )

    app.add_handler(
        CallbackQueryHandler(
            market_callback,
            pattern=r"^mkt:"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_buttons
        )
    )

    logger.info(
        "OTC Smart Analyzer Telegram bot starting..."
    )

    app.run_polling()


if __name__ == "__main__":
    main()
