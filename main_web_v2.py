# main_web_v2.py — Telegram webhook-приложение (python-telegram-bot 21.4)
# Работает только в заданной теме форума (forum topic) по env:
#   ALLOWED_CHAT_ID=-1002904857758
#   ALLOWED_TOPIC_ID=4
# И использует модерацию/процессор для генерации QR.

import os
import logging
from typing import Optional

from telegram import Update, Document, Message
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters
)

from keyboards import moderation_keyboard
from moderation import handle_moderation, handle_reason_message
from store import store
from processor import on_approved_send_qr

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("main_web")

TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # например https://invoice-bot-xxx.onrender.com/webhook
PORT = int(os.getenv("PORT", "10000"))

# Ограничение области работы (только эта тема)
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))     # например -1002904857758
ALLOWED_TOPIC_ID = int(os.getenv("ALLOWED_TOPIC_ID", "0"))   # например 4

def _allowed_topic(update: Update) -> bool:
    msg: Optional[Message] = update.effective_message
    if not msg:
        return False
    chat = update.effective_chat
    if not chat:
        return False
    cid = chat.id
    tid = msg.message_thread_id  # None для обычных чатов/PM
    if ALLOWED_CHAT_ID and cid != ALLOWED_CHAT_ID:
        return False
    if ALLOWED_TOPIC_ID and tid != ALLOWED_TOPIC_ID:
        return False
    return True

# ---------- helpers ----------
def _detect_file_type(doc: Document) -> str:
    # document | excel
    name = (doc.file_name or "").lower()
    mt = (doc.mime_type or "").lower()
    if name.endswith((".xlsx", ".xls", ".csv")) or ("excel" in mt):
        return "excel"
    return "document"

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # В личке отвечаем; в группе — только если внутри разрешённой темы
    if update.effective_chat and update.effective_chat.type == "private":
        await update.message.reply_text("Бот на связи ✅\nПришлите счёт в нужной теме группы.")
        return
    if not _allowed_topic(update):
        return
    await update.message.reply_text("Бот на связи в этой теме ✅")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed_topic(update):
        return
    msg = update.effective_message
    photo = msg.photo[-1]
    file = await context.bot.get_file(photo.file_id)  # не скачиваем сейчас
    # ставим статус и кнопки
    text = "📄 Счёт получен — Ожидает согласования"
    m = await msg.reply_text(text, reply_markup=moderation_keyboard(), reply_to_message_id=msg.message_id)
    # сохраняем источник
    store.put(m.message_id, {
        "src": {
            "file_id": photo.file_id,
            "file_type": "photo",
            "thread_id": msg.message_thread_id
        }
    })

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed_topic(update):
        return
    msg = update.effective_message
    doc = msg.document
    if not doc:
        return
    file_type = _detect_file_type(doc)
    text = "📄 Счёт получен — Ожидает согласования"
    m = await msg.reply_text(text, reply_markup=moderation_keyboard(), reply_to_message_id=msg.message_id)
    store.put(m.message_id, {
        "src": {
            "file_id": doc.file_id,
            "file_type": file_type,
            "thread_id": msg.message_thread_id
        }
    })

# moderation (approve/decline + причины)
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed_topic(update):
        return
    await handle_moderation(update, context)

async def on_reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Сообщение-пояснение по причине отказа (мы помечаем пользователя в moderation.py)
    if not _allowed_topic(update):
        return
    await handle_reason_message(update, context)

def main() -> None:
    if not TOKEN or not WEBHOOK_URL:
        raise RuntimeError("BOT_TOKEN or WEBHOOK_URL is not set")

    app = Application.builder().token(TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start_cmd))

    # документы и фото
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    # кнопки модерации
    app.add_handler(CallbackQueryHandler(on_callback))

    # текстовые пояснения (после "Отклонить → Указать причину")
    # фильтр — обычный текст (в moderation.py вы отмечаете ожидание причины на пользователя)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_reason_text))

    # запуск webhook
    log.info("Setting webhook to: %s", WEBHOOK_URL)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
