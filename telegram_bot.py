
import asyncio
import logging
import json
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
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


def format_duration(signal):
    duration = signal.get(
        "selected_duration_seconds",
        signal.get("duration_seconds", 60)
    )

    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 60

    if duration == 120:
        label = "دقيقتان (120 ثانية)"
    else:
        label = "دقيقة واحدة (60 ثانية)"

    reason = signal.get(
        "duration_reason",
        "اختيار تجريبي حسب خصائص الإشارة"
    )

    return label, reason


def signal_phase(signal):
    return signal.get("phase") or signal.get("status")


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
                    status = str(event.get("status", "")).upper()
                    updates[signal_id].add(status)

                elif event_type == "result":
                    results[signal_id] = event

                elif event_type == "horizon_result":
                    try:
                        horizon = int(event.get("horizon_seconds"))
                    except (TypeError, ValueError):
                        continue

                    outcome = str(event.get("outcome", "")).upper()

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
                outcome = str(event.get("outcome", "")).upper()

                if outcome in ("WIN", "LOSS", "DRAW"):
                    stats[duration][outcome] += 1

        result_60 = horizons.get(60)
        result_120 = horizons.get(120)

        if result_60 and result_120:
            outcome_60 = str(result_60.get("outcome", "")).upper()
            outcome_120 = str(result_120.get("outcome", "")).upper()

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
        outcome = str(result.get("outcome", "")).upper()

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

    horizon_stats, paired = build_horizon_stats(horizon_results)

    lines.append(format_horizon_line(60, horizon_stats[60]))
    lines.append(format_horizon_line(120, horizon_stats[120]))

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
            total = stats["WIN"] + stats["LOSS"] + stats["DRAW"]
            decisive = stats["WIN"] + stats["LOSS"]
            rate = stats["WIN"] / decisive * 100 if decisive else 0

            lines.append(
                f"\n• {strategy}\n"
                f"  {stats['WIN']}W / {stats['LOSS']}L / "
                f"{stats['DRAW']}D | الفوز {rate:.1f}% | "
                f"الإجمالي {total}"
            )
    else:
        lines.append("\nلا توجد بيانات استراتيجية مكتملة بعد.")

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
            key=lambda item: item[1]["WIN"] + item[1]["LOSS"],
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
        lines.append("\nلا توجد نتائج أسواق محفوظة بعد.")

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    subscribers = context.application.bot_data.setdefault(
        "subscribers", set()
    )

    subscribers.add(chat_id)

    await update.message.reply_text(
        "🤖 OTC Smart Analyzer\n\n"
        "✅ تم تسجيلك لاستقبال الإشارات تلقائياً\n"
        "🔍 مراقبة أفضل 5 أدوات OTC\n"
        "⏱️ اختيار تجريبي لمدة الصفقة: 60 أو 120 ثانية\n"
        "📊 مقارنة نتائج المدتين\n"
        "🧠 استراتيجيات مستقلة متعددة\n"
        "📈 التحليل على فريم الدقيقة فقط\n\n"
        "/status - حالة البوت\n"
        "/analyze - تحليل يدوي\n"
        "/testsymbols - قائمة الأدوات\n"
        "/stats - إحصائيات الأداء\n\n"
        "⚠️ جميع النتائج تجريبية وليست ضماناً للربح."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.application.bot_data.get("scanner_task")
    running = task is not None and not task.done()

    subscribers = context.application.bot_data.get(
        "subscribers", set()
    )

    await update.message.reply_text(
        "📊 حالة OTC Smart Analyzer\n\n"
        f"الماسح: {'🟢 يعمل' if running else '🔴 متوقف'}\n"
        "الأدوات المستهدفة: 5\n"
        "فريم التحليل: دقيقة واحدة فقط\n"
        "الدخول المقترح: بداية دقيقة UTC\n"
        "اختيار مدة الصفقة: 60 أو 120 ثانية\n"
        "مقارنة النتائج: المدتان معاً\n"
        f"المشتركون الحاليون: {len(subscribers)}\n\n"
        "⚠️ اختيار المدة تجريبي والنتائج افتراضية فقط."
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 جاري قراءة سجل الصفقات..."
    )

    try:
        data = await asyncio.to_thread(read_trade_history)
        message = build_stats_message(data)
        await update.message.reply_text(message)

    except Exception:
        logger.exception("Statistics generation failed")
        await update.message.reply_text(
            "❌ تعذر قراءة سجل الصفقات. راجع سجلات التشغيل."
        )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0] if context.args else None

    await update.message.reply_text("🔍 جاري التحليل على فريم الدقيقة...")

    try:
        symbols = await asyncio.to_thread(fetch_otc_symbols)

        if symbol:
            item = next(
                (
                    s for s in symbols
                    if s["symbol"].lower() == symbol.lower()
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

        message = (
            "📡 تحليل يدوي تجريبي\n\n"
            f"الأداة: {selected}\n"
            f"الفريم: 1 دقيقة\n"
            f"الاتجاه: {signal['direction']}\n"
            f"السعر المرجعي: {signal['price']}\n\n"
            f"{format_signal_details(signal)}\n\n"
            "⚠️ هذا التحليل ليس إشارة دخول مباشرة، "
            "ولا يتضمن اختيار مدة الصفقة الآلي."
        )

        await update.message.reply_text(message)

    except Exception:
        logger.exception("Manual analysis failed")
        await update.message.reply_text(
            "❌ تعذر التحليل. راجع سجلات التشغيل."
        )


async def testsymbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        symbols = await asyncio.to_thread(fetch_otc_symbols)

        symbols = [
            item for item in symbols
            if item.get("isOtc") is True
            and item.get("open") is True
        ]

        def payout_value(item):
            try:
                return float(item.get("payout") or 0)
            except (TypeError, ValueError):
                return 0

        symbols.sort(key=payout_value, reverse=True)

        await update.message.reply_text(
            "أفضل 5 أدوات OTC المفتوحة:\n\n" +
            "\n".join(
                f"{item['symbol']} | {item.get('payout')}%"
                for item in symbols[:5]
            )
        )

    except Exception:
        logger.exception("Symbol list failed")
        await update.message.reply_text(
            "❌ تعذر جلب قائمة الأدوات. راجع سجلات التشغيل."
        )


async def send_to_subscribers(app, message):
    subscribers = list(
        app.bot_data.get("subscribers", set())
    )

    if not subscribers:
        logger.warning("No Telegram subscribers available")
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


async def post_init(app):
    app.bot_data["subscribers"] = set()

    async def notify(signal):
        phase = signal_phase(signal)
        strategies_text = format_signal_details(signal)

        duration_label, duration_reason = format_duration(signal)

        if phase == "PRE_ALERT":
            entry_time = (
                signal.get("entry_time")
                or "غير محدد"
            )

            confirm_time = signal.get(
                "confirm_time",
                "قبل الدخول بـ15 ثانية"
            )

            message = (
                "🔔 تنبيه فرصة OTC قادمة\n\n"
                f"الأداة: {signal.get('symbol', 'غير محدد')}\n"
                f"الاتجاه المتوقع: {signal.get('direction', 'غير محدد')}\n"
                f"وقت التنبيه: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n"
                f"موعد التأكيد: {confirm_time}\n"
                f"موعد الدخول المقترح: {entry_time}\n\n"
                f"⏱️ المدة المختارة تجريبياً: {duration_label}\n"
                f"🧠 سبب اختيار المدة: {duration_reason}\n\n"
                f"{strategies_text}\n\n"
                "⏳ سيعيد البوت فحص الإشارة قبل الدخول.\n"
                "إذا تم التأكيد، سيكون الدخول عند بداية الدقيقة المحددة.\n"
                "لا تدخل إذا فاتك التوقيت أو لم تستطع التنفيذ."
            )

        elif phase == "ENTRY":
            entry_time = signal.get("entry_time", "غير محدد")
            exit_time = signal.get("exit_time", "غير محدد")
            entry_price = signal.get(
                "price",
                signal.get("entry_price", "غير محدد")
            )

            message = (
                "🚨 تأكيد دخول تجريبي\n\n"
                f"الأداة: {signal.get('symbol', 'غير محدد')}\n"
                f"الاتجاه: {signal.get('direction', 'غير محدد')}\n\n"
                f"🕒 وقت الدخول المحدد: {entry_time}\n"
                f"⏱️ المدة المختارة: {duration_label}\n"
                f"🧠 سبب الاختيار: {duration_reason}\n"
                f"🏁 وقت انتهاء الصفقة: {exit_time}\n"
                f"💹 السعر المرجعي: {entry_price}\n\n"
                f"{strategies_text}\n\n"
                "📊 سيقارن البوت داخلياً نتيجة 60 و120 ثانية، "
                "لكن النتيجة الأساسية ستكون للمدة المختارة.\n\n"
                "⚠️ هذه صفقة افتراضية وليست أمراً للتداول الحقيقي."
            )

        elif phase == "CANCELLED":
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

        else:
            logger.error(
                "Unknown signal phase/status: %s",
                phase
            )
            return False

        return await send_to_subscribers(app, message)

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

        duration = result.get("duration_seconds", 60)

        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 60

        duration_label = (
            "دقيقتان (120 ثانية)"
            if duration == 120
            else "دقيقة واحدة (60 ثانية)"
        )

        duration_reason = result.get(
            "duration_reason",
            "اختيار تجريبي حسب خصائص الإشارة"
        )

        message = (
            "📊 نتيجة الصفقة التجريبية\n\n"
            f"{labels.get(result['result'], result['result'])}\n\n"
            f"الأداة: {result['symbol']}\n"
            f"الاتجاه: {result['direction']}\n"
            f"المدة المختارة: {duration_label}\n"
            f"سبب الاختيار: {duration_reason}\n"
            f"سعر البداية: {result['entry_price']}\n"
            f"سعر النهاية: {result['exit_price']}\n"
            f"وقت البداية: {result['entry_time']}\n"
            f"وقت النهاية: {result['exit_time']}\n\n"
            "الاستراتيجيات:\n"
            f"{strategy_text}\n\n"
            "⚠️ النتيجة محسوبة من أسعار OTCharts، "
            "وليست تسوية Pocket Option الفعلية."
        )

        return await send_to_subscribers(app, message)

    app.bot_data["scanner_task"] = asyncio.create_task(
        scanner_loop(notify, notify_result)
    )

    logger.info("Automatic scanner started")


async def post_shutdown(app):
    task = app.bot_data.get("scanner_task")

    if task:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("testsymbols", testsymbols))
    app.add_handler(CommandHandler("stats", stats))

    logger.info("OTC Smart Analyzer starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
