# keyboards.py — динамические кнопки по статусу
from __future__ import annotations
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from store import store, WAIT, APPROVED, REJECTED, PAID, RECEIVED

APPROVE_CB  = "approve"
REJECT_CB   = "reject"
REASON_CB   = "reason"
PAID_CB     = "paid"
RECEIVED_CB = "received"

def moderation_keyboard(chat_id: int, status_msg_id: int):
    inv = store.get(status_msg_id) or {"status": WAIT}
    st = inv.get("status", WAIT)
    rows: list[list[InlineKeyboardButton]] = []

    if st == WAIT:
        rows = [[
            InlineKeyboardButton("✔️ Согласовать", callback_data=f"{APPROVE_CB}:{chat_id}:{status_msg_id}"),
            InlineKeyboardButton("✖️ Отклонить",  callback_data=f"{REJECT_CB}:{chat_id}:{status_msg_id}"),
        ]]
    elif st == APPROVED:
        rows = [[
            InlineKeyboardButton("💳 Оплачен", callback_data=f"{PAID_CB}:{chat_id}:{status_msg_id}"),
        ]]
    elif st == REJECTED:
        rows = [[
            InlineKeyboardButton("📝 Указать причину", callback_data=f"{REASON_CB}:{chat_id}:{status_msg_id}"),
        ]]
    elif st == PAID:
        rows = [[
            InlineKeyboardButton("✅ Получен", callback_data=f"{RECEIVED_CB}:{chat_id}:{status_msg_id}"),
        ]]
    elif st == RECEIVED:
        rows = []  # финал — без кнопок

    return InlineKeyboardMarkup(rows) if rows else None

