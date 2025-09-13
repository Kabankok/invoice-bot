# moderation.py — обработка нажатий и причины отклонения
from __future__ import annotations
import os
from typing import Dict, Tuple
from telegram import Update
from telegram.ext import ContextTypes

from store import store, WAIT, APPROVED, REJECTED, PAID, RECEIVED
from keyboards import (
    moderation_keyboard,
    APPROVE_CB, REJECT_CB, REASON_CB, PAID_CB, RECEIVED_CB,
)

ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()}

# user_id -> (chat_id, status_msg_id) — ожидаем одно текстовое сообщение с причиной
WAITING_REASON: Dict[int, Tuple[int, int]] = {}


def _human_status(code: str) -> str:
    return {
        WAIT: "Ожидает согласования",
        APPROVED: "Согласован",
        REJECTED: "Отклонён",
        PAID: "Оплачен",
        RECEIVED: "Получен",
    }.get(code, code)


def build_status_text(inv: dict) -> str:
    status = _human_status(inv.get("status", WAIT))
    reason = inv.get("reason") or ""
    lines = ["📄 Счёт", f"Статус: {status}"]
    if inv.get("status") == REJECTED and reason:
        lines.append(f"Причина: {reason}")
    return "
".join(lines)


async def handle_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    user_id = q.from_user.id if q.from_user else 0
    if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
        await q.reply_text("⛔ У вас нет прав на эту операцию.")
        return

    data = (q.data or "").split(":")
    action = data[0] if data else ""
    chat_id = q.message.chat_id
    status_msg_id = q.message.message_id  # кнопки живут на статусном сообщении

    inv = store.get(status_msg_id) or {"status": WAIT, "reason": ""}

    if action == APPROVE_CB:
        store.set_status(status_msg_id, APPROVED)
    elif action == REJECT_CB:
        store.set_status(status_msg_id, REJECTED)
    elif action == REASON_CB:
        WAITING_REASON[user_id] = (chat_id, status_msg_id)
        await q.message.reply_text("📝 Напишите одной строкой причину отклонения этого счёта.")
        return
    elif action == PAID_CB:
        store.set_status(status_msg_id, PAID)
    elif action == RECEIVED_CB:
        store.set_status(status_msg_id, RECEIVED)
    else:
        await q.reply_text("Неизвестное действие")
        return

    inv = store.get(status_msg_id) or inv
    await q.edit_message_text(
        text=build_status_text(inv),
        reply_markup=moderation_keyboard(chat_id, status_msg_id),
    )


async def handle_reason_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    pending = WAITING_REASON.get(user.id)
    if not pending:
        return  # для других сообщений ничего не делаем

    chat_id, status_msg_id = pending
    reason_text = (update.effective_message.text or "").strip()
    store.set_reason(status_msg_id, reason_text)

    inv = store.get(status_msg_id) or {}
    WAITING_REASON.pop(user.id, None)

    # Обновляем карточку
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg_id,
        text=build_status_text(inv),
        reply_markup=moderation_keyboard(chat_id, status_msg_id),
    )
    await update.effective_message.reply_text("Причина сохранена ✅")
