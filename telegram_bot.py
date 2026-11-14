
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
        "📊 متابعة نتائج الصفقات مفعّلة\n\n"
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
        f"المشتركون الحاليون: {len(subscribers)}\n\n"
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
            asyncio.to_thread(fetch_candles, selected, 60, 50),
            asyncio.to_thread(fetch_candles, selected, 300, 50)
        )

        signal = analyze_signal(candles_1m, candles_5m)

        if not signal:
            await update.message.reply_text(
                f"📊 {selected}\n"
                "لا توجد فرصة مطابقة للشروط حالياً."
            )
            return

        await update.message.reply_text(
            "📡 تحليل يدوي تجريبي\n\n"
            f"الأداة: {selected}\n"
            f"الاتجاه: {signal['direction']}\n"
            f"النمط: {signal['pattern']}\n"
            f"السعر المرجعي: {signal['entry_reference_price']}\n\n"
            "⚠️ هذا التحليل ليس إشارة دخول مباشرة."
        )

    except Exception as error:
        logger.exception("Manual analysis failed")
        await update.message.reply_text(
            f"❌ تعذر التحليل: {str(error)[:300]}"
        )


async def testsymbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        symbols = await asyncio.to_thread(fetch_otc_symbols)

        symbols.sort(
            key=lambda item: float(item.get("payout") or 0),
            reverse=True
        )

        await update.message.reply_text(
            "أفضل 5 أدوات OTC:\n\n" +
            "\n".join(
                f"{item['symbol']} | {item.get('payout')}%"
                for item in symbols[:5]
            )
        )

    except Exception as error:
        logger.exception("Symbol list failed")
        await update.message.reply_text(
            f"❌ خطأ: {str(error)[:300]}"
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
                f"النمط: {signal['pattern']}\n"
                f"وقت التنبيه: {signal['alert_time']}\n"
                f"موعد الدخول المقترح: "
                f"{signal['planned_entry_time']}\n\n"
                "⏳ الدخول المقترح بعد 60 ثانية.\n"
                "سيعيد البوت فحص الشروط قبل تأكيد الدخول.\n"
                "لا تدخل اعتماداً على هذا التنبيه وحده."
            )

        elif phase == "ENTRY":
            message = (
                "🚨 تأكيد دخول تجريبي\n\n"
                f"الأداة: {signal['symbol']}\n"
                f"الاتجاه: {signal['direction']}\n"
                f"النمط: {signal['pattern']}\n"
                f"وقت الدخول الافتراضي: {signal['entry_time']}\n"
                f"السعر المرجعي: {signal['entry_price']}\n"
                "مدة الصفقة التجريبية: دقيقة واحدة\n\n"
                "⚠️ هذه صفقة افتراضية وليست أمراً "
                "للتداول الحقيقي."
            )

        elif phase == "CANCELLED":
            message = (
                "❌ تم إلغاء فرصة OTC\n\n"
                f"الأداة: {signal['symbol']}\n"
                f"الاتجاه السابق: {signal['direction']}\n"
                f"موعد الدخول الذي كان مقترحاً: "
                f"{signal['planned_entry_time']}\n"
                f"السبب: {signal['cancel_reason']}\n\n"
                "لم يتم تسجيل صفقة لهذه الفرصة."
            )

        else:
            logger.error("Unknown signal phase: %s", phase)
            return False

        return await send_to_subscribers(app, message)

    async def notify_result(result):
        labels = {
            "WIN": "🟢 WIN — ربح افتراضي",
            "LOSS": "🔴 LOSS — خسارة افتراضية",
            "DRAW": "🟡 DRAW — تعادل"
        }

        message = (
            "📊 نتيجة الصفقة التجريبية\n\n"
            f"{labels.get(result['result'], result['result'])}\n\n"
            f"الأداة: {result['symbol']}\n"
            f"الاتجاه: {result['direction']}\n"
            f"سعر البداية: {result['entry_price']}\n"
            f"سعر النهاية: {result['exit_price']}\n"
            f"وقت البداية: {result['entry_time']}\n"
            f"وقت النهاية: {result['exit_time']}\n\n"
            "⚠️ النتيجة محسوبة من أسعار OTCharts، "
            "وليست نتيجة تسوية Pocket Option الفعلية."
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
