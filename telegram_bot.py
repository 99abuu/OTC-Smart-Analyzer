
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from config import TELEGRAM_BOT_TOKEN
from market_data import fetch_candles


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 OTC Smart Analyzer\n\n"
        "البوت جاهز لاستقبال أوامرك.\n\n"
        "الأوامر:\n"
        "/start - بدء البوت\n"
        "/status - حالة البوت\n"
        "/analyze - طلب تحليل\n"
        "/testdata - اختبار بيانات OTC"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 حالة البوت: يعمل\n"
        "📈 الفريمات: 1m و 5m\n"
        "🔍 اختبار بيانات السوق: لم يتم التحقق بعد"
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏳ لم نفعّل إصدار الإشارات بعد.\n"
        "استخدم /testdata أولاً للتحقق من اتصال بيانات السوق."
    )


async def testdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري اختبار اتصال OTCharts...")

    try:
        candles = fetch_candles(
            symbol="EURUSD_otc",
            timeframe=60,
            limit=5
        )

        latest = candles[-1]

        await update.message.reply_text(
            "✅ تم استلام بيانات OTC بنجاح!\n\n"
            f"عدد الشموع: {len(candles)}\n"
            "الزوج: EUR/USD OTC\n"
            "الفريم: دقيقة واحدة\n\n"
            f"آخر شمعة:\n"
            f"Open: {latest['open']}\n"
            f"High: {latest['high']}\n"
            f"Low: {latest['low']}\n"
            f"Close: {latest['close']}"
        )

    except Exception as error:
        print(f"OTCharts test failed: {error}")
        await update.message.reply_text(
            "❌ لم ينجح الاتصال ببيانات OTCharts.\n"
            "راجع سجل التشغيل لمعرفة سبب الخطأ."
        )


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("testdata", testdata))

    print("OTC Smart Analyzer is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
