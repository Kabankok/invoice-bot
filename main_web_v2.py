#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from store import store_invoice
from keyboards import moderation_keyboard
from moderation import handle_moderation, handle_reason_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("main_web_v2")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", "10000"))
if not TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is missing")
if not WEBHOOK_URL:
    raise SystemExit("WEBHOOK_URL is missing (e.g. https://<name>.onrender.com/webhook)")

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Привет, {user.first_name}! Бот на связи ✅")

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Webhook: {WEBHOOK_URL}")

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Твой user_id: {user.id}")

# --- Обработка файлов ---

def _detect_kind(msg) -> str:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "document", None):
        mt = (msg.document.mime_type or "").lower()
        if mt in {"application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}:
            return "excel"
        return "document"
    return "unknown"

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_in = update.message
    kind = _detect_kind(msg_in)

    sent = await msg_in.reply_text(
        f"📄 Счёт получен — Ожидает согласования
Тип: {kind}",
        reply_markup=moderation_keyboard(msg_in.chat_id, msg_in.message_id)  # временно ставим по входному id
    )
    # В качестве ключа храним ИД статусного сообщения (бота)
    store_invoice(sent.message_id, status="WAIT", kind=kind)

# --- Main ---
def main():
    app = Application.builder().token(TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler("whoami", whoami))

    # файлы
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))

    # кнопки модерации
    app.add_handler(CallbackQueryHandler(handle_moderation))

    # приём причины отклонения (одно текстовое сообщение от админа)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reason_message))

    # запуск вебхука
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
