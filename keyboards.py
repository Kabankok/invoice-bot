from __future__ import annotations
from typing import Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


APPROVE_CB = "approve"
REJECT_CB = "reject"


def approval_keyboard(chat_id: int, message_id: int) -> InlineKeyboardMarkup:
payload_ok = f"{APPROVE_CB}:{chat_id}:{message_id}"
payload_rej = f"{REJECT_CB}:{chat_id}:{message_id}"
return InlineKeyboardMarkup([
[
InlineKeyboardButton("✔️ Согласовать", callback_data=payload_ok),
InlineKeyboardButton("✖️ Отклонить", callback_data=payload_rej),
]
])


def parse_callback(data: str) -> Tuple[str, int, int]:
parts = (data or "").split(":")
if len(parts) != 3:
raise ValueError("bad callback format")
action, chat_s, msg_s = parts
return action, int(chat_s), int(msg_s)