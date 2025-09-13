#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main_web.py — Шаг 1 (MVP-вебхук): подключаем бота к чату и теме, проверяем, что
мы видим вложения и умеем отвечать. Без OCR/GPT/QR — только приём и эхо-ответ.

Структура по блокам (чтобы легко чинить):
A) Конфиг и константы
B) Хелперы (проверки чата/темы, форматирование)
C) Хендлеры Telegram (start, документы/фото/Excel)
D) Сборка и запуск приложения (webhook)

Деплой (Render → Web Service):
- Команда запуска: `python main_web.py`
- Переменные окружения: TELEGRAM_BOT_TOKEN, WEBHOOK_URL, ALLOWED_CHAT_ID, ALLOWED_THREAD_ID, ADMIN_USER_IDS
- Порт: Render передаёт в переменной PORT; мы его читаем автоматически

После деплоя: отправьте в нужную тему любой тестовый файл — бот ответит и
в логах вы увидите chat_id и message_thread_id (их впишем в .env позже).
"""
from __future__ import annotations
import os
import json
import logging
from typing import Optional

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================ A) Конфиг и константы ============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main_web")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # например: https://your-service.onrender.com/webhook
PORT = int(os.getenv("PORT", "10000"))      # Render пробрасывает порт сюда

# Ограничения (заполним по факту):
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))        # 0 = любой чат
ALLOWED_THREAD_ID = int(os.getenv("ALLOWED_THREAD_ID", "0"))      # 0 = любая тема
ADMIN_USER_IDS = {
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}  # кто сможет потом жать кнопки согласования и т.п.

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")

# ====================== B) Хелперы: проверки чата/темы =========================
async def is_allowed(update: Update) -> bool:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return False

    # тип чата
    if chat.type not in (ChatType.SUPERGROUP, ChatType.GROUP, ChatType.PRIVATE):
        return False

    # фильтр по chat_id
    if ALLOWED_CHAT_ID and chat.id != ALLOWED_CHAT_ID:
        return False

    # фильтр по теме (message_thread_id появляется в супергруппах с темами)
    thread_id = getattr(msg, "message_thread_id", None)
    if ALLOWED_THREAD_ID and thread_id != ALLOWED_THREAD_ID:
        return False

    return True

# ====================== C) Хендлеры Telegram (минимум) ========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    await update.message.reply_text(
        "Бот на связи ✅\nПришлите PDF/изображение/Excel в нужной теме — отвечу и запишу ID темы."
    )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return

    msg = update.effective_message
    chat = update.effective_chat
    thread_id = getattr(msg, "message_thread_id", None)

    # Логируем базовые ID — пригодится, чтобы заполнить .env
    log.info("Got file in chat_id=%s thread_id=%s from user_id=%s", chat.id, thread_id, msg.from_user.id if msg.from_user else None)

    # Определяем тип вложения (для Excel — это Document с соответствующим mime)
    kind = "document"
    if msg.photo:
        kind = "photo"
    elif msg.document:
        # минимальная классификация для Excel
        mime = (msg.document.mime_type or "").lower()
        if mime in {"application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}:
            kind = "excel"
        else:
            kind = "document"

    # Короткий ответ в тему (ничего не обрабатываем на этом шаге)
    text = (
        "✅ Получил файл.\n"
        f"Тип: {kind}\n"
        f"chat_id: {chat.id}\n"
        f"message_thread_id: {thread_id}\n"
        "Это шаг 1 (проверка вебхука). OCR/GPT/QR добавим на следующих шагах."
    )
    await msg.reply_text(text)

# ======================== D) Сборка/запуск (webhook) ==========================
async def _post_init(app):
    """Вызывается после app.initialize(). Ставит вебхук на адрес WEBHOOK_URL."""
    if not WEBHOOK_URL:
        log.warning("WEBHOOK_URL не задан. Установите URL сервиса Render в переменную окружения.")
        return
    await app.bot.set_webhook(url=WEBHOOK_URL)
    log.info("Webhook set to: %s", WEBHOOK_URL)


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    # Роутинг команд и вложений (минимальная версия)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))

    # Запускаем встроенный веб-сервер PTB (aiohttp) на порту Render
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL or None,  # если задан — установит вебхук автоматически
        secret_token=None,                # можно добавить позже
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
