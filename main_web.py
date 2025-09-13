#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main_web_v2.py — модульная версия с кнопками модерации.
Использует ENV:
  TELEGRAM_BOT_TOKEN
  WEBHOOK_URL (полный URL вида https://<name>.onrender.com/webhook)
  PORT (опционально, по умолчанию 10000)

Зависимости:
  python-telegram-bot[webhooks]==21.4
Файлы-модули (рядом с этим файлом):
  store.py         — простое хранилище в памяти (store, store_invoice)
  keyboards.py     — клавиатуры (moderation_keyboard, APPROVE_CB/REJECT_CB)
  moderation.py    — обработчик нажатий (handle_moderation)
"""

from __future__ import annotations

import os
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# наши модули
from store import store_invoice
from keyboards import moderation_keyboard
from moderation import handle_moderation

# ------------------------- Логирование -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main_web_v2")

# ------------------------- ENV -------------------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", "10000"))

if not TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is missing")
if not WEBHOOK_URL:
    raise SystemExit("WEBHOOK_URL is missing (e.g. https://<name>.onrender.com/webhook)")

# ------------------------- Команды -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message or update.channel_post
    if not chat or not msg:
        return
    thread_id = getattr(msg, "message_thread_id", None)
    text = (
        "Бот на связи ✅\n"
        "Пришлите PDF/изображение/Excel — добавлю кнопки согласования.\n"
        f"(chat_id={chat.id}, thread_id={thread_id})"
    )
    if hasattr(msg, "reply_text"):
        await msg.reply_text(text)
    else:
        # для каналов
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

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    uid = user.id if user else None
    await update.effective_message.reply_text(f"Ваш user_id: {uid}")

# ------------------------- Утилиты -------------------------
def detect_kind_from_message(msg) -> str:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "document", None):
        mime = (msg.document.mime_type or "").lower()
        if mime in {
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }:
            return "excel"
        return "document"
    return "unknown"

# ------------------------- Обработка файлов -------------------------
async def handle_file_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Файлы из ЛС/групп/супергрупп/тем (message)."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return

    thread_id = getattr(msg, "message_thread_id", None)
    kind = detect_kind_from_message(msg)
    key_chat_id = chat.id
    key_message_id = msg.message_id

    log.info(
        "Got FILE(message) | chat_id=%s thread_id=%s user_id=%s kind=%s",
        chat.id,
        thread_id,
        getattr(getattr(msg, "from_user", None), "id", None),
        kind,
    )

    # сохраняем в простое хранилище по message_id (MVP)
    store_invoice(key_message_id, status="pending")

    text = (
        "📄 Счёт получен.\n"
        "Статус: Ожидает согласования\n"
        f"Тип файла: {kind}\n"
        f"chat_id: {key_chat_id}, message_id: {key_message_id}\n"
        "Нажмите кнопку ниже."
    )
    sent = await msg.reply_text(
        text,
        reply_markup=moderation_keyboard(key_chat_id, key_message_id),
    )
    # в этой версии v2 мы не редактируем «статусное» сообщение повторно из хранилища,
    # модуль moderation.py заменяет текст текущего сообщения по коллбэку.

async def handle_file_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Файлы из каналов (channel_post). Бот должен быть админом канала."""
    chat = update.effective_chat
    post = update.channel_post
    if not chat or not post:
        return

    kind = detect_kind_from_message(post)
    key_chat_id = chat.id
    key_message_id = post.message_id

    log.info("Got FILE(channel_post) | chat_id=%s kind=%s", chat.id, kind)

    store_invoice(key_message_id, status="pending")

    text = (
        "📄 Счёт получен в канале.\n"
        "Статус: Ожидает согласования\n"
        f"Тип файла: {kind}\n"
        f"chat_id: {key_chat_id}, message_id: {key_message_id}\n"
        "Нажмите кнопку ниже."
    )
    await context.bot.send_message(
        chat_id=key_chat_id,
        text=text,
        reply_markup=moderation_keyboard(key_chat_id, key_message_id),
    )

# ------------------------- Старт приложения -------------------------
async def _post_init(app):
    me = await app.bot.get_me()
    log.info("Bot getMe: username=@%s id=%s", me.username, me.id)

def main() -> None:
    app = ApplicationBuilder().token(TOKEN).post_init(_post_init).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("whoami", cmd_whoami))

    # Кнопки модерации (коллбэк)
    app.add_handler(CallbackQueryHandler(handle_moderation))

    # Файлы: группы/темы/ЛС
    app.add_handler(
        MessageHandler(
            (filters.Document.ALL | filters.PHOTO) & ~filters.ChatType.CHANNEL,
            handle_file_message,
        )
    )
    # Файлы: каналы
    app.add_handler(
        MessageHandler(
            (filters.Document.ALL | filters.PHOTO) & filters.ChatType.CHANNEL,
            handle_file_channel,
        )
    )

    # Вебхук
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",         # сервер слушает ровно этот путь
        webhook_url=WEBHOOK_URL,     # полный URL из ENV
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
