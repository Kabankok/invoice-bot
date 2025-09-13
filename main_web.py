#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main_web.py — Шаг 2 (модерация кнопками: ✔️ Согласовать / ✖️ Отклонить)
-----------------------------------------------------------------------
MVP-логика модерации прямо в одном файле (позже вынесем в keyboards.py/moderation.py):
- При получении файла бот отвечает в тему и добавляет инлайн-клавиатуру.
- По нажатию кнопок бот проверяет, что нажал админ (из ADMIN_USER_IDS),
  проставляет статус и редактирует сообщение.
- Журнал статусов хранится в памяти процесса (для теста). На проде вынесем в store.py/БД.

ENV, которые нужно выставить на Render → Environment:
- TELEGRAM_BOT_TOKEN
- WEBHOOK_URL = https://<имя>.onrender.com/webhook
- ADMIN_USER_IDS = 111111111,222222222   (список Telegram user_id, у кого есть право жать кнопки)

requirements.txt:
  python-telegram-bot[webhooks]==21.4
"""
from __future__ import annotations
import os
import logging
from typing import Dict, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

# Список админов, которым разрешено согласовывать/отклонять
ADMIN_USER_IDS = {
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")

# ============================ Память процесса (MVP) ===========================
# Ключ счёта: (chat_id, message_id). Значение: словарь со статусом и типом.
INVOICES: Dict[Tuple[int, int], Dict[str, str]] = {}

STATUS_WAIT = "Ожидает согласования"
STATUS_OK = "Согласован"
STATUS_REJ = "Отклонён"

# ============================ Хендлеры команд =================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message or update.channel_post
    if not chat or not msg:
        return
    thread_id = getattr(msg, "message_thread_id", None)
    text = (
        "Бот на связи ✅
"
        "Пришлите PDF/изображение/Excel — добавлю кнопки согласования.
"
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
        "🔎 Webhook debug:
"
        f"bot: @{me.username} (id={me.id})
"
        f"url: {info.url or '—'}
"
        f"pending_update_count: {info.pending_update_count}
"
        f"admins: {sorted(list(ADMIN_USER_IDS))}"
    )
    msg = update.effective_message or update.channel_post
    chat = update.effective_chat
    if msg and hasattr(msg, "reply_text"):
        await msg.reply_text(text)
    elif chat:
        await context.bot.send_message(chat_id=chat.id, text=text)

# ============================ Кнопки и коллбэки ===============================
APPROVE_CB = "approve"
REJECT_CB = "reject"

def approval_keyboard(invoice_key: Tuple[int, int]) -> InlineKeyboardMarkup:
    chat_id, msg_id = invoice_key
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✔️ Согласовать", callback_data=f"{APPROVE_CB}:{chat_id}:{msg_id}"),
            InlineKeyboardButton("✖️ Отклонить", callback_data=f"{REJECT_CB}:{chat_id}:{msg_id}"),
        ]
    ])

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    user_id = q.from_user.id if q.from_user else 0
    if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
        await q.reply_text("⛔ У вас нет прав на эту операцию.")
        return

    data = q.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await q.reply_text("Некорректные данные кнопки")
        return

    action, chat_id_s, msg_id_s = parts
    try:
        key = (int(chat_id_s), int(msg_id_s))
    except ValueError:
        await q.reply_text("Некорректные идентификаторы")
        return

    invoice = INVOICES.get(key)
    if not invoice:
        await q.reply_text("Счёт не найден (возможно, перезапуск сервиса)")
        return

    if action == APPROVE_CB:
        invoice["status"] = STATUS_OK
        new_caption = build_status_caption(invoice)
        await context.bot.edit_message_text(
            chat_id=key[0],
            message_id=invoice["status_msg_id"],
            text=new_caption,
        )
        await q.reply_text("✅ Согласовано. Дальше подключим GPT/QR на следующем шаге.")
    elif action == REJECT_CB:
        invoice["status"] = STATUS_REJ
        new_caption = build_status_caption(invoice)
        await context.bot.edit_message_text(
            chat_id=key[0],
            message_id=invoice["status_msg_id"],
            text=new_caption,
        )
        await q.reply_text("🛑 Отклонено. Причины/комментарии добавим позже.")

# ============================ Обработка файлов ================================
async def handle_file_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return

    thread_id = getattr(msg, "message_thread_id", None)
    kind = detect_kind_from_message(msg)
    key = (chat.id, msg.message_id)

    INVOICES[key] = {"status": STATUS_WAIT, "kind": kind}

    log.info(
        "Got FILE(message) | chat_id=%s thread_id=%s user_id=%s kind=%s",
        chat.id,
        thread_id,
        getattr(getattr(msg, 'from_user', None), 'id', None),
        kind,
    )

    text = (
        "📄 Счёт получен.
"
        f"Статус: {STATUS_WAIT}
"
        f"Тип файла: {kind}
"
        f"chat_id: {chat.id}, message_id: {msg.message_id}
"
        "Нажмите кнопку ниже."
    )
    sent = await msg.reply_text(text, reply_markup=approval_keyboard(key))
    INVOICES[key]["status_msg_id"] = sent.message_id

async def handle_file_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    post = update.channel_post
    if not chat or not post:
        return

    kind = detect_kind_from_message(post)
    key = (chat.id, post.message_id)
    INVOICES[key] = {"status": STATUS_WAIT, "kind": kind}

    log.info("Got FILE(channel_post) | chat_id=%s kind=%s", chat.id, kind)

    text = (
        "📄 Счёт получен в канале.
"
        f"Статус: {STATUS_WAIT}
"
        f"Тип файла: {kind}
"
        f"chat_id: {chat.id}, message_id: {post.message_id}
"
        "Нажмите кнопку ниже."
    )
    sent = await context.bot.send_message(chat_id=chat.id, text=text, reply_markup=approval_keyboard(key))
    INVOICES[key]["status_msg_id"] = sent.message_id

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

def build_status_caption(invoice: Dict[str, str]) -> str:
    status = invoice.get("status", "?")
    kind = invoice.get("kind", "?")
    return (
        "📄 Счёт
"
        f"Статус: {status}
"
        f"Тип файла: {kind}
"
        "(Журнал/комментарии добавим позже)"
    )

# ========================= Запуск приложения ==================================
async def _post_init(app):
    me = await app.bot.get_me()
    log.info("Bot getMe: username=@%s id=%s", me.username, me.id)


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("debug", cmd_debug))

    # Кнопочные коллбэки
    app.add_handler(CallbackQueryHandler(on_callback))

    # Файлы из групп/супергрупп/ЛС
    app.add_handler(MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.ChatType.CHANNEL, handle_file_message))

    # Файлы из КАНАЛОВ (channel_post)
    app.add_handler(MessageHandler((filters.Document.ALL | filters.PHOTO) & filters.ChatType.CHANNEL, handle_file_channel))

    # Запуск вебхука
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=WEBHOOK_URL or None,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
