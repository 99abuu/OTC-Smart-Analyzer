
import asyncio
import logging

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

        lines.append(
            f"• {name}\n  السبب: {reason}"
        )

    return "\n".join(lines)


def format_signal_details(signal):
    return (
        f"📚 الاستراتيجيات المتحققة "
        f"({len(signal.get('matching_strategies', []))}):\n"
        f"{format_strategies(signal)}"
    )


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
        "⏱️ تنبيه قبل الدخول بـ60 ثانية\n"
        "📊 متابعة نتائج الصفقات مفعّلة\n"
        "🧠 استراتيجيات مستقلة متعددة\n\n"
        "/status - حالة البوت\n"
        "/analyze - تحليل يدوي\n"
        "/testsymbols - قائمة الأدوات\n\n"
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
        "التنبيه المسبق: 60 ثانية\n"
        f"المشتركون الحاليون: {len(subscribers)}\n"
        "نظام التحليل: استراتيجيات مستقلة\n\n"
        "⚠️ النتائج تجريبية فقط."
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0] if context.args else None

    await update.message.reply_text("🔍 جاري التحليل...")

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
            item = symbols[0] if symbols else None

        if not item:
            await update.message.reply_text(
                "لم يتم العثور على الأداة."
            )
            return

        selected = item["symbol"]

        candles_1m, candles_5m = await asyncio.gather(
            asyncio.to_thread(fetch_candles, selected, 60, 300),
            asyncio.to_thread(fetch_candles, selected, 300, 300)
        )

        signal = analyze_signal(
            candles_1m,
            candles_5m,
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
            f"الاتجاه: {signal['direction']}\n"
            f"السعر المرجعي: {signal['price']}\n\n"
            f"{format_signal_details(signal)}\n\n"
            "⚠️ هذا التحليل ليس إشارة دخول مباشرة."
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
            "أفضل 5 أدوات OTC:\n\n" +
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
        phase = signal.get("phase")

        if phase == "PRE_ALERT":
            message = (
                "🔔 تنبيه فرصة OTC قادمة\n\n"
                f"الأداة: {signal['symbol']}\n"
                f"الاتجاه المتوقع: {signal['direction']}\n"
                f"وقت التنبيه: {signal['alert_time']}\n"
                f"موعد الدخول المقترح: {signal['planned_entry_time']}\n\n"
                f"{format_signal_details(signal)}\n\n"
                "⏳ الدخول المقترح بعد 60 ثانية.\n"
                "سيعيد البوت فحص الاستراتيجية قبل تأكيد الدخول.\n"
                "لا تدخل اعتماداً على هذا التنبيه وحده."
            )

        elif phase == "ENTRY":
            message = (
                "🚨 تأكيد دخول تجريبي\n\n"
                f"الأداة: {signal['symbol']}\n"
                f"الاتجاه: {signal['direction']}\n"
                f"وقت الدخول الافتراضي: {signal['entry_time']}\n"
                f"السعر المرجعي: {signal['entry_price']}\n\n"
                f"{format_signal_details(signal)}\n\n"
                "مدة الصفقة التجريبية: دقيقة واحدة\n\n"
                "⚠️ هذه صفقة افتراضية وليست أمراً للتداول الحقيقي."
            )

        elif phase == "CANCELLED":
            message = (
                "❌ تم إلغاء فرصة OTC\n\n"
                f"الأداة: {signal['symbol']}\n"
                f"الاتجاه السابق: {signal['direction']}\n"
                f"موعد الدخول: {signal['planned_entry_time']}\n"
                f"السبب: {signal['cancel_reason']}\n\n"
                f"{format_signal_details(signal)}\n\n"
                "لم يتم تسجيل صفقة لهذه الفرصة."
            )

        else:
            logger.error("Unknown signal phase: %s", phase)
            return False

        return await send_to_subscribers(app, message)

    async def notify_result(result):
        labels = {
            "WIN": "🟢 WIN — نتيجة افتراضية",
            "LOSS": "🔴 LOSS — نتيجة افتراضية",
            "DRAW": "🟡 DRAW — تعادل"
        }

        strategies = result.get("strategies", [])

        message = (
            "📊 نتيجة الصفقة التجريبية\n\n"
            f"{labels.get(result['result'], result['result'])}\n\n"
            f"الأداة: {result['symbol']}\n"
            f"الاتجاه: {result['direction']}\n"
            f"سعر البداية: {result['entry_price']}\n"
            f"سعر النهاية: {result['exit_price']}\n"
            f"وقت البداية: {result['entry_time']}\n"
            f"وقت النهاية: {result['exit_time']}\n\n"
            "الاستراتيجيات:\n"
            + "\n".join(f"• {s}" for s in strategies)
            + "\n\n⚠️ النتيجة محسوبة من أسعار OTCharts، "
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

    logger.info("OTC Smart Analyzer starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
