#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main_web.py — Шаг 1.3 (фикс синтаксиса строк)
--------------------------------------------
Ошибка была в том, что многострочные строки не были закрыты. Исправил: 
теперь строки формируются с использованием склеивания и `\n`.

requirements.txt:
  python-telegram-bot[webhooks]==21.4
"""
from __future__ import annotations
import os
import logging
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================ Конфиг/логирование ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main_web")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # например: https://<name>.onrender.com/webhook
PORT = int(os.getenv("PORT", "10000"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")

# ============================ Хендлеры ========================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message or update.channel_post
    if not chat or not msg:
        return
    thread_id = getattr(msg, "message_thread_id", None)
    text = (
        "Бот на связи ✅\n"
        "Пришлите PDF/изображение/Excel — отвечу и запишу ID.\n"
        f"(chat_id={chat.id}, thread_id={thread_id})"
    )
    if hasattr(msg, "reply_text"):
        await msg.reply_text(text)
    else:
        await context.bot.send_message(chat_id=chat.id, text=text)

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    info = await context.bot.get_webhook_info()
    me = await context.bot.get_me()
    text = (
        "🔎 Webhook debug:\n"
        f"bot: @{me.username} (id={me.id})\n"
        f"url: {info.url or '—'}\n"
        f"pending_update_count: {info.pending_update_count}\n"
    )
    msg = update.effective_message or update.channel_post
    chat = update.effective_chat
    if msg and hasattr(msg, "reply_text"):
        await msg.reply_text(text)
    elif chat:
        await context.bot.send_message(chat_id=chat.id, text=text)

async def handle_file_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Файлы из групп/супергрупп/ЛС (message)."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    thread_id = getattr(msg, "message_thread_id", None)
    kind = detect_kind_from_message(msg)
    log.info(
        "Got FILE(message) | chat_id=%s thread_id=%s user_id=%s kind=%s",
        chat.id,
        thread_id,
        getattr(getattr(msg, 'from_user', None), 'id', None),
        kind,
    )
    text = (
        "✅ Получил файл.\n"
        f"Тип: {kind}\n"
        f"chat_id: {chat.id}\n"
        f"message_thread_id: {thread_id}\n"
        "Это шаг 1 (проверка вебхука). OCR/GPT/QR на следующих шагах."
    )
    await msg.reply_text(text)

async def handle_file_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Файлы из КАНАЛОВ (channel_post). Бот должен быть админом канала."""
    chat = update.effective_chat
    post = update.channel_post
    if not chat or not post:
        return
    kind = detect_kind_from_message(post)
    log.info("Got FILE(channel_post) | chat_id=%s kind=%s", chat.id, kind)
    text = (
        "✅ Получил файл в канале.\n"
        f"Тип: {kind}\n"
        f"chat_id: {chat.id}\n"
        "Это шаг 1 (webhook OK). Для модерации/кнопок лучше использовать супергруппу с темами."
    )
    await context.bot.send_message(chat_id=chat.id, text=text)

async def log_everything(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chat = update.effective_chat
        msg = update.effective_message or update.channel_post
        thread_id = getattr(msg, "message_thread_id", None) if msg else None
        log.info(
            "Got UPDATE | type=%s chat_type=%s chat_id=%s thread_id=%s",
            type(update).__name__, getattr(chat, 'type', None), getattr(chat, 'id', None), thread_id,
        )
    except Exception as e:
        log.warning("log_everything error: %s", e)

# ============================ Утилиты =========================================
def detect_kind_from_message(msg) -> str:
    if getattr(msg, 'photo', None):
        return 'photo'
    if getattr(msg, 'document', None):
        mime = (msg.document.mime_type or '').lower()
        if mime in {"application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}:
            return 'excel'
        return 'document'
    return 'unknown'

# ========================= Запуск приложения ==================================
async def _post_init(app):
    me = await app.bot.get_me()
    log.info("Bot getMe: username=@%s id=%s", me.username, me.id)


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("debug", cmd_debug))

    # Файлы из групп/супергрупп/ЛС
    app.add_handler(MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.ChatType.CHANNEL, handle_file_message))

    # Файлы из КАНАЛОВ (channel_post)
    app.add_handler(MessageHandler((filters.Document.ALL | filters.PHOTO) & filters.ChatType.CHANNEL, handle_file_channel))

    # Лог всего остального
    app.add_handler(MessageHandler(filters.ALL, log_everything))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=WEBHOOK_URL or None,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
