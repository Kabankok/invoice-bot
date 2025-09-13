#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main_web.py — Шаг 1 (фикс: нет aiohttp)
---------------------------------------
Теперь убрали зависимость от aiohttp. Используем только встроенный webhook-сервер PTB.
Добавлено:
- Ответ на /start в ЛС и в группе/теме.
- Лог всех апдейтов.
- Лог getMe при старте.

Деплой: заменить этот файл, убедиться что в requirements.txt стоит:
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # https://<name>.onrender.com/webhook
PORT = int(os.getenv("PORT", "10000"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")

# ============================ Хендлеры ========================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    thread_id = getattr(msg, "message_thread_id", None)
    await msg.reply_text(
        "Бот на связи ✅\nПришлите PDF/изображение/Excel в нужной теме — отвечу и запишу ID темы.\n"
        f"(chat_id={chat.id}, thread_id={thread_id})"
    )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    thread_id = getattr(msg, "message_thread_id", None)

    kind = "document"
    if msg.photo:
        kind = "photo"
    elif msg.document:
        mime = (msg.document.mime_type or "").lower()
        if mime in {"application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}:
            kind = "excel"
        else:
            kind = "document"

    log.info("Got FILE | chat_id=%s thread_id=%s user_id=%s kind=%s", chat.id, thread_id, msg.from_user.id if msg.from_user else None, kind)

    await msg.reply_text(
        "✅ Получил файл.\n"
        f"Тип: {kind}\n"
        f"chat_id: {chat.id}\n"
        f"message_thread_id: {thread_id}\n"
        "Это шаг 1 (проверка вебхука). OCR/GPT/QR добавим на следующих шагах."
    )

async def log_everything(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chat = update.effective_chat
        msg = update.effective_message
        thread_id = getattr(msg, "message_thread_id", None) if msg else None
        log.info("Got UPDATE | type=%s chat_id=%s thread_id=%s user_id=%s", type(update).__name__, getattr(chat, 'id', None), thread_id, getattr(getattr(msg, 'from_user', None), 'id', None))
    except Exception as e:
        log.warning("log_everything error: %s", e)

# ========================= Запуск приложения ==================================
async def _post_init(app):
    me = await app.bot.get_me()
    log.info("Bot getMe: username=@%s id=%s", me.username, me.id)
    if WEBHOOK_URL:
        await app.bot.set_webhook(url=WEBHOOK_URL)
        log.info("Webhook set to: %s", WEBHOOK_URL)
    else:
        log.warning("WEBHOOK_URL not set; set it to your Render URL + /webhook")

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.ALL, log_everything))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL or None,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()


