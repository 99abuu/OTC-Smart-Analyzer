
import json

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import TELEGRAM_BOT_TOKEN
from market_data import fetch_candles, fetch_otc_symbols


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 OTC Smart Analyzer\n\n"
        "الأوامر:\n"
        "/start - بدء البوت\n"
        "/status - حالة البوت\n"
        "/analyze - طلب تحليل\n"
        "/testdata - اختبار EUR/USD OTC\n"
        "/testsymbols - اختبار قائمة OTC\n"
        "/testapple - اختبار Apple OTC"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 حالة البوت: يعمل\n"
        "📈 الفريمات: 1m و 5m\n"
        "🔍 بيانات السوق: قيد الاختبار"
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏳ لم يتم تفعيل إصدار الإشارات بعد.\n"
        "نختبر بيانات جميع أدوات OTC أولاً."
    )


async def testdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري اختبار EUR/USD OTC...")

    try:
        candles = fetch_candles(
            symbol="EURUSD_otc",
            timeframe=60,
            limit=5
        )

        latest = candles[-1]

        await update.message.reply_text(
            "✅ اتصال الشموع ناجح!\n\n"
            f"عدد الشموع: {len(candles)}\n"
            f"Open: {latest['open']}\n"
            f"High: {latest['high']}\n"
            f"Low: {latest['low']}\n"
            f"Close: {latest['close']}"
        )

    except Exception as error:
        print(f"Candle test failed: {error}")
        await update.message.reply_text(
            f"❌ فشل اختبار الشموع:\n{str(error)[:500]}"
        )


async def testsymbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري جلب قائمة OTC...")

    try:
        symbols = fetch_otc_symbols()

        await update.message.reply_text(
            f"✅ نجح الاتصال!\n"
            f"عدد الأدوات المتاحة: {len(symbols)}\n\n"
            "أول 5 أدوات:\n"
            + "\n".join(
                f"{item['symbol']} | {item['payout']}%"
                for item in symbols[:5]
            )
        )

    except Exception as error:
        print(f"Symbols test failed: {error}")
        await update.message.reply_text(
            f"❌ فشل جلب القائمة:\n{str(error)[:500]}"
        )


async def testapple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏳ جاري اختبار شموع Apple OTC على فريم الدقيقة..."
    )

    try:
        candles = fetch_candles(
            symbol="#AAPL_otc",
            timeframe=60,
            limit=5
        )

        latest = candles[-1]

        await update.message.reply_text(
            "✅ نجح اختبار Apple OTC!\n\n"
            f"عدد الشموع: {len(candles)}\n"
            f"Open: {latest['open']}\n"
            f"High: {latest['high']}\n"
            f"Low: {latest['low']}\n"
            f"Close: {latest['close']}"
        )

    except Exception as error:
        print(f"Apple OTC test failed: {error}")
        await update.message.reply_text(
            f"❌ فشل اختبار Apple OTC:\n{str(error)[:500]}"
        )


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("testdata", testdata))
    app.add_handler(CommandHandler("testsymbols", testsymbols))
    app.add_handler(CommandHandler("testapple", testapple))

    print("OTC Smart Analyzer is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
