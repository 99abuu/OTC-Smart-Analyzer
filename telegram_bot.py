
import asyncio
import json

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import TELEGRAM_BOT_TOKEN
from market_data import (
    fetch_candles,
    fetch_otc_symbols,
    stream_ticks
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 OTC Smart Analyzer\n\n"
        "/start - بدء البوت\n"
        "/status - حالة البوت\n"
        "/analyze - طلب تحليل\n"
        "/testdata - اختبار EUR/USD OTC\n"
        "/testsymbols - اختبار قائمة OTC\n"
        "/testapple - اختبار Apple OTC\n"
        "/teststream - اختبار البث المباشر"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 البوت يعمل\n"
        "📡 البث المباشر: قيد الاختبار\n"
        "🔍 الإشارات: لم يتم تفعيلها بعد"
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏳ نظام الإشارات قيد البناء."
    )


async def testdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏳ جاري اختبار EUR/USD OTC..."
    )

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
    await update.message.reply_text(
        "⏳ جاري جلب قائمة OTC..."
    )

    try:
        symbols = fetch_otc_symbols()

        await update.message.reply_text(
            f"✅ نجح الاتصال!\n"
            f"عدد الأدوات المتاحة: {len(symbols)}\n\n"
            "أول 5 أدوات:\n"
            + "\n".join(
                f"{item['symbol']} | {item.get('payout')}%"
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
        "⏳ جاري اختبار Apple OTC..."
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
        print(f"Apple test failed: {error}")
        await update.message.reply_text(
            f"❌ فشل اختبار Apple OTC:\n{str(error)[:500]}"
        )


def read_first_tick(symbols):
    stream = stream_ticks(symbols)

    try:
        for event in stream:
            if event.get("_event") == "tick":
                return event

    finally:
        stream.close()

    return None


async def teststream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏳ جاري اختبار البث المباشر...\n"
        "تم تحديد خمس أدوات، وننتظر أول تحديث سعر."
    )

    try:
        available = fetch_otc_symbols()

        selected = [
            item["symbol"]
            for item in available[:5]
        ]

        if not selected:
            raise RuntimeError("No available OTC symbols")

        event = await asyncio.wait_for(
            asyncio.to_thread(read_first_tick, selected),
            timeout=60
        )

        if event is None:
            raise RuntimeError("No tick event received")

        await update.message.reply_text(
            "✅ وصل أول تحديث سعر مباشر!\n\n"
            f"الأدوات: {', '.join(selected)}\n\n"
            "بيانات التحديث:\n"
            + json.dumps(event, ensure_ascii=False)[:2500]
        )

    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⚠️ لم يصل تحديث سعر خلال 60 ثانية.\n"
            "هذا لا يعني أن الاتصال فشل، لكنه يعني أننا لم نتحقق من وصول tick بعد."
        )

    except Exception as error:
        print(f"Stream test failed: {error}")
        await update.message.reply_text(
            f"❌ فشل اختبار البث:\n{str(error)[:500]}"
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
    app.add_handler(CommandHandler("teststream", teststream))

    print("OTC Smart Analyzer is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
