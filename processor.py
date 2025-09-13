# processor.py — отправка файла в GPT (опционально) и генерация QR-кода
from __future__ import annotations
import io
import os
import logging
from typing import Tuple

import qrcode  # требует pillow
from telegram.ext import ContextTypes

from store import store, APPROVED

log = logging.getLogger("processor")

# =============== QR ===============
def _qr_png_bytes(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# =============== GPT (опционально) ===============
# Пример заглушки: если нет OPENAI_API_KEY, просто возвращаем стандартный payload.
# Если подключишь реальный парсинг, тут можно разобрать PDF/Excel/изображение и собрать платёжную строку.

async def _extract_payment_payload(file_bytes: bytes, file_type: str) -> Tuple[str, str]:
    """
    Возвращает (payload_for_qr, human_caption).
    Сейчас заглушка: payload — просто текст с суммой 0.00; caption — «Согласован».
    """
    # TODO: сюда добавим разбор через GPT/vision по желанию
    payload = "PAYMENT|AMOUNT=0.00|CURRENCY=RUB|DESC=Invoice"
    caption = "QR для оплаты (демо). После интеграции GPT сюда подставим сумму/назначение."
    return payload, caption

# =============== Основной шаг после APPROVE ===============
async def on_approved_send_qr(context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, status_msg_id: int) -> None:
    inv = store.get(status_msg_id)
    if not inv or not inv.get("src"):
        log.warning("No source bound to status_msg_id=%s", status_msg_id)
        return

    src = inv["src"]
    file_id = src["file_id"]
    file_type = src["file_type"]
    thread_id = src.get("thread_id")

    # 1) качаем файл из Telegram
    tg_file = await context.bot.get_file(file_id)
    fb = await tg_file.download_as_bytearray()

    # 2) парсим (пока заглушка) и генерим QR
    payload, caption = await _extract_payment_payload(bytes(fb), file_type)
    png = _qr_png_bytes(payload)

    # 3) шлём QR в ту же тему/чат
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=png,
        caption=caption,
        message_thread_id=thread_id if thread_id else None,
        reply_to_message_id=status_msg_id,
    )

    # доп. действие — можем уточнить статус (он уже APPROVED) или переотправить клавиатуру при необходимости
    log.info("QR sent for status_msg_id=%s", status_msg_id)