# keyboards.py
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

APPROVE_CB = "approve"
REJECT_CB = "reject"

# Функция для генерации клавиатуры модерации

def moderation_keyboard(chat_id: int = 0, message_id: int = 0):
    payload_ok = f"{APPROVE_CB}:{chat_id}:{message_id}"
    payload_no = f"{REJECT_CB}:{chat_id}:{message_id}"

    keyboard = [
        [
            InlineKeyboardButton("✔️ Согласовать", callback_data=payload_ok),
            InlineKeyboardButton("✖️ Отклонить", callback_data=payload_no),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)
