
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from config import TELEGRAM_BOT_TOKEN

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 OTC Smart Analyzer\n\n"
        "البوت جاهز لاستقبال أوامرك.\n\n"
        "الأوامر المتاحة:\n"
        "/start - بدء البوت\n"
        "/status - حالة البوت\n"
        "/analyze - طلب تحليل\n"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 حالة البوت: قيد التطوير\n"
        "📈 الفريمات المستهدفة: 1m و 5m\n"
        "🔍 تحليل السوق: غير متصل حالياً"
    )

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏳ لا توجد بيانات شموع متصلة حتى الآن.\n"
        "لن أصدر CALL أو PUT قبل توفير بيانات سوق حقيقية."
    )

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analyze", analyze))

    print("OTC Smart Analyzer is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
