# moderation.py
from telegram import Update
from telegram.ext import ContextTypes
from store import store

APPROVE_CB = "approve"
REJECT_CB = "reject"

async def handle_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[0]
    chat_id = int(data[1])
    message_id = int(data[2])

    invoice = store.get(message_id)
    status = invoice.get("status", "?") if invoice else "?"

    if action == APPROVE_CB:
        store.update(message_id, "approved")
        new_status = "✅ Согласован"
    elif action == REJECT_CB:
        store.update(message_id, "rejected")
        new_status = "❌ Отклонён"
    else:
        new_status = status

    try:
        await query.edit_message_text(
            text=f"Статус счёта обновлён: {new_status}"
        )
    except Exception as e:
        await query.message.reply_text(f"Ошибка обновления статуса: {e}")


