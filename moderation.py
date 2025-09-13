from __future__ import annotations
import os
from typing import Set, Tuple
from telegram import Update
from telegram.ext import ContextTypes


from store import STORE, STATUS_OK, STATUS_REJ
from keyboards import parse_callback, APPROVE_CB, REJECT_CB


# Разрешённые пользователи (админы), которые могут нажимать кнопки
ADMIN_USER_IDS: Set[int] = {
int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}


def build_status_caption(invoice: dict) -> str:
status = invoice.get("status", "?")
kind = invoice.get("kind", "?")
return (
"📄 Счёт\n"
f"Статус: {status}\n"
f"Тип файла: {kind}\n"
"(Журнал/комментарии добавим позже)"
)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
q = update.callback_query
if not q:
return
await q.answer()


user_id = q.from_user.id if q.from_user else 0
if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
await q.reply_text("⛔ У вас нет прав на эту операцию.")
return


try:
action, chat_id, msg_id = parse_callback(q.data or "")
except Exception:
await q.reply_text("Некорректные данные кнопки")
return


key = (chat_id, msg_id)
invoice = STORE.get(key)
if not invoice:
await q.reply_text("Счёт не найден (возможно, сервис перезапускался)")
return


if action == APPROVE_CB:
invoice["status"] = STATUS_OK
elif action == REJECT_CB:
invoice["status"] = STATUS_REJ
else:
await q.reply_text("Неизвестное действие")
return


status_msg_id = invoice.get("status_msg_id")
if isinstance(status_msg_id, int):
await context.bot.edit_message_text(
chat_id=chat_id,
message_id=status_msg_id,
text=build_status_caption(invoice),
)
await q.reply_text("Готово ✅")