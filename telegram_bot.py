
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
from signal_engine import analyze_signal


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 OTC Smart Analyzer\n\n"
        "/start - بدء البوت\n"
        "/status - حالة البوت\n"
        "/analyze - تحليل أول أداة متاحة\n"
        "/analyze #AAPL_otc - تحليل أداة محددة\n"
        "/testdata - اختبار EUR/USD OTC\n"
        "/testsymbols - قائمة أدوات OTC\n"
        "/testapple - اختبار Apple OTC\n"
        "/teststream - اختبار البث المباشر"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 البوت يعمل\n"
        "📡 اتصال OTCharts: تم اختباره\n"
        "🔍 محرك التحليل: مفعّل\n"
        "⚠️ الإشارات تجريبية وليست ضماناً للربح"
    )


def run_analysis(symbol=None):
    available = fetch_otc_symbols()

    if not available:
        raise RuntimeError("لا توجد أدوات OTC متاحة حالياً")

    if symbol:
        match = next(
            (
                item for item in available
                if item["symbol"].lower() == symbol.lower()
            ),
            None
        )
        if match is None:
            raise RuntimeError(
                "الأداة غير موجودة في قائمة OTC المتاحة"
            )
    else:
        match = available[0]

    selected_symbol = match["symbol"]

    candles_1m = fetch_candles(
        symbol=selected_symbol,
        timeframe=60,
        limit=50
    )

    candles_5m = fetch_candles(
        symbol=selected_symbol,
        timeframe=300,
        limit=50
    )

    signal = analyze_signal(candles_1m, candles_5m)

    return selected_symbol, signal, len(candles_1m), len(candles_5m)


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0] if context.args else None

    await update.message.reply_text(
        "🔍 جاري تحليل بيانات OTC...\n"
        "انتظر قليلاً."
    )

    try:
        selected, signal, count_1m, count_5m = await asyncio.to_thread(
            run_analysis,
            symbol
        )

        if signal is None:
            await update.message.reply_text(
                f"📊 نتيجة التحليل\n\n"
                f"الأداة: {selected}\n"
                f"شموع الدقيقة: {count_1m}\n"
                f"شموع 5 دقائق: {count_5m}\n\n"
                "لا توجد فرصة مطابقة للشروط حالياً.\n"
                "لن نصدر إشارة لمجرد إجبار النظام على الدخول."
            )
            return

        direction = signal["direction"]
        emoji = "🟢" if direction == "CALL" else "🔴"

        await update.message.reply_text(
            f"📡 إشارة تجريبية\n\n"
            f"الأداة: {selected}\n"
            f"الاتجاه: {emoji} {direction}\n"
            f"النمط: {signal['pattern']}\n"
            f"السعر المرجعي: {signal['entry_reference_price']}\n"
            f"الإطار المؤكد: 5 دقائق\n"
            f"إطار الإشارة: دقيقة واحدة\n"
            f"الانتهاء المقترح: دقيقة واحدة\n\n"
            "⚠️ هذه نتيجة تحليل أولي وليست توصية مالية.\n"
            "اختبرها على الحساب التجريبي فقط."
        )

    except Exception as error:
        print(f"Analysis failed: {error}")
        await update.message.reply_text(
            f"❌ تعذر إكمال التحليل:\n{str(error)[:400]}"
        )


async def testdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري اختبار EUR/USD OTC...")

    try:
        candles = await asyncio.to_thread(
            fetch_candles,
            "EURUSD_otc",
            60,
            5
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
        symbols = await asyncio.to_thread(fetch_otc_symbols)

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
    await update.message.reply_text("⏳ جاري اختبار Apple OTC...")

    try:
        candles = await asyncio.to_thread(
            fetch_candles,
            "#AAPL_otc",
            60,
            5
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
        "ننتظر أول تحديث سعر."
    )

    try:
        available = await asyncio.to_thread(fetch_otc_symbols)

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
            "✅ وصل تحديث سعر مباشر!\n\n"
            f"الأدوات: {', '.join(selected)}\n\n"
            + json.dumps(event, ensure_ascii=False)[:2500]
        )

    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⚠️ لم يصل تحديث سعر خلال 60 ثانية."
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
