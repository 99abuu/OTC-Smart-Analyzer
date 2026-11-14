
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

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    subscribers = context.application.bot_data.setdefault(
        "subscribers", []
    )

    if chat_id not in subscribers:
        subscribers.append(chat_id)

    await update.message.reply_text(
        "🤖 OTC Smart Analyzer\n\n"
        "✅ تم تسجيلك لاستقبال الإشارات تلقائياً\n"
        "🔍 مراقبة أفضل 5 أدوات OTC\n"
        "⏱️ دورة الفحص كل 3 دقائق\n\n"
        "/status - حالة البوت\n"
        "/analyze - تحليل يدوي\n"
        "/testsymbols - قائمة الأدوات\n\n"
        "⚠️ الإشارات تجريبية وليست ضماناً للربح."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.application.bot_data.get("scanner_task")

    running = task is not None and not task.done()

    await update.message.reply_text(
        "📊 حالة OTC Smart Analyzer\n\n"
        f"الماسح: {'🟢 يعمل' if running else '🔴 متوقف'}\n"
        "الأدوات المستهدفة: 5\n"
        "الفاصل المستهدف: 3 دقائق\n"
        "استقبال الإشارات: مفعّل عند /start\n\n"
        "⚠️ النتائج تجريبية فقط."
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0] if context.args else None

    await update.message.reply_text("🔍 جاري التحليل...")

    try:
        symbols = await asyncio.to_thread(fetch_otc_symbols)

        if symbol:
            item = next(
                (s for s in symbols
                 if s["symbol"].lower() == symbol.lower()),
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

        candles_1m = await asyncio.to_thread(
            fetch_candles, selected, 60, 50
        )

        candles_5m = await asyncio.to_thread(
            fetch_candles, selected, 300, 50
        )

        signal = analyze_signal(candles_1m, candles_5m)

        if not signal:
            await update.message.reply_text(
                f"📊 {selected}\n"
                "لا توجد فرصة مطابقة للشروط حالياً."
            )
            return

        await update.message.reply_text(
            "📡 إشارة تجريبية\n\n"
            f"الأداة: {selected}\n"
            f"الاتجاه: {signal['direction']}\n"
            f"النمط: {signal['pattern']}\n"
            f"السعر المرجعي: {signal['entry_reference_price']}\n"
            "الانتهاء المقترح: دقيقة واحدة\n\n"
            "⚠️ اختبرها على الحساب التجريبي فقط."
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
        await update.message.reply_text(
            f"❌ خطأ: {str(error)[:300]}"
        )


async def post_init(app):
    async def notify(signal):
        message = (
            "🚨 فرصة OTC تجريبية\n\n"
            f"الأداة: {signal['symbol']}\n"
            f"الاتجاه: {signal['direction']}\n"
            f"النمط: {signal['pattern']}\n"
            f"السعر المرجعي: {signal['entry_reference_price']}\n"
            "الانتهاء المقترح: دقيقة واحدة\n\n"
            "⚠️ إشارة آلية أولية. اختبرها تجريبياً فقط."
        )

        for chat_id in list(
            app.bot_data.get("subscribers", [])
        ):
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=message
                )
            except Exception:
                logger.exception(
                    "Could not send signal to chat %s",
                    chat_id
                )

    app.bot_data["subscribers"] = []

    app.bot_data["scanner_task"] = asyncio.create_task(
        scanner_loop(notify)
    )

    logger.info("Automatic scanner task started")


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
