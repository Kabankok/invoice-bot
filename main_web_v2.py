#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from store import store_invoice, store
from keyboards import moderation_keyboard
from moderation import handle_moderation, handle_reason_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
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

# --- Вспомогательное ---
def _detect_kind_and_ftype(msg) -> tuple[str, str]:
    # kind — человекочитаемый тип, ftype — внутренний для процессора
    if getattr(msg, "photo", None):
        return "photo", "photo"

    if getattr(msg, "document", None):
        mt = (msg.document.mime_type or "").lower()
        if mt in {
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel.sheet.macroenabled.12",
            "application/vnd.ms-excel.sheet.binary.macroenabled.12",
        }:
            return "excel", "excel"
        if mt in {"application/pdf"}:
            return "pdf", "document"
        # по умолчанию: документ, но процессор воспримет как PDF-текст → не идеально
        return "document", "document"

    return "unknown", "document"

# --- Обработка файлов ---
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_in = update.message
    chat = msg_in.chat
    thread_id = getattr(msg_in, "message_thread_id", None)
    kind, ftype = _detect_kind_and_ftype(msg_in)

    # 1) создаём статусное сообщение БОТА (на нём будут кнопки/статус)
    text = f"""📄 Счёт получен — Ожидает согласования
Тип: {kind}"""
    sent = await msg_in.reply_text(text, reply_markup=moderation_keyboard(chat.id, 0))

    # 2) фиксируем запись в хранилище по ИД статусного сообщения БОТА
    store_invoice(sent.message_id, status="WAIT", kind=kind)

    # 3) привяжем исходный файл к карточке (для шага QR)
    if msg_in.document:
        file_id = msg_in.document.file_id
    elif msg_in.photo:
        file_id = msg_in.photo[-1].file_id  # максимальное качество
    else:
        file_id = ""

    store.set_source(
        sent.message_id,
        chat_id=chat.id,
        thread_id=thread_id,
        user_msg_id=msg_in.message_id,
        file_id=file_id,
        file_type=ftype,  # <-- тут теперь "excel" / "document" / "photo"
    )

    # 4) перерисуем клавиатуру уже с корректным status_msg_id
    await context.bot.edit_message_reply_markup(
        chat_id=sent.chat_id,
        message_id=sent.message_id,
        reply_markup=moderation_keyboard(sent.chat_id, sent.message_id),
    )

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

    # приём причины отклонения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reason_message))

    # запуск вебхука
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
