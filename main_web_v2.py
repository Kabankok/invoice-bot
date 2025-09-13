import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from store import store_invoice
from keyboards import moderation_keyboard
from moderation import handle_moderation

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL") + "/webhook"
ADMIN_IDS = [int(uid) for uid in os.environ.get("ADMIN_USER_IDS", "").split(",") if uid]

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Привет, {user.first_name}! Бот на связи ✅")

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Webhook: {WEBHOOK_URL}\nAdmin IDs: {ADMIN_IDS}"
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Твой user_id: {user.id}")

# --- Обработка файлов ---
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = update.message.effective_attachment
    msg = await update.message.reply_text(
        f"📄 Счёт получен (тип: {file.file_unique_id}) — Ожидает согласования",
        reply_markup=moderation_keyboard()
    )
    store_invoice(msg.message_id, status="pending")

# --- Main ---
def main():
    app = Application.builder().token(TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler("whoami", whoami))

    # файлы
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))

    # кнопки
    app.add_handler(CallbackQueryHandler(handle_moderation))

    # запуск вебхука
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path="webhook",
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
