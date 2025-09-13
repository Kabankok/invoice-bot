#!/usr/bin/env python3
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user = update.effective_user
uid = user.id if user else None
await update.effective_message.reply_text(f"Ваш user_id: {uid}")


# --- Утилиты ---
def detect_kind_from_message(msg) -> str:
if getattr(msg, "photo", None): return "photo"
if getattr(msg, "document", None):
mime = (msg.document.mime_type or "").lower()
if mime in {
"application/vnd.ms-excel",
"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}: return "excel"
return "document"
return "unknown"


# --- Обработка файлов ---
async def handle_file_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
chat = update.effective_chat
msg = update.effective_message
if not chat or not msg: return


thread_id = getattr(msg, "message_thread_id", None)
kind = detect_kind_from_message(msg)
key = (chat.id, msg.message_id)


STORE.put_invoice(key, kind)
log.info("Got FILE(message) | chat_id=%s thread_id=%s user_id=%s kind=%s",
chat.id, thread_id, getattr(getattr(msg, 'from_user', None), 'id', None), kind)


text = ("📄 Счёт получен.\n"
f"Статус: {STATUS_WAIT}\n"
f"Тип файла: {kind}\n"
f"chat_id: {chat.id}, message_id: {msg.message_id}\n"
"Нажмите кнопку ниже.")


sent = await msg.reply_text(text, reply_markup=approval_keyboard(chat.id, msg.message_id))
STORE.set_status_msg_id(key, sent.message_id)


async def handle_file_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
chat = update.effective_chat
post = update.channel_post
if not chat or not post: return


kind = detect_kind_from_message(post)
key = (chat.id, post.message_id)
STORE.put_invoice(key, kind)


log.info("Got FILE(channel_post) | chat_id=%s kind=%s", chat.id, kind)


text = ("📄 Счёт получен в канале.\n"
f"Статус: {STATUS_WAIT}\n"
f"Тип файла: {kind}\n"
f"chat_id: {chat.id}, message_id: {post.message_id}\n"
"Нажмите кнопку ниже.")


sent = await context.bot.send_message(chat_id=chat.id, text=text,
reply_markup=approval_keyboard(chat.id, post.message_id))
STORE.set_status_msg_id(key, sent.message_id)


# --- Запуск ---
async def _post_init(app):
me = await app.bot.get_me()
log.info("Bot getMe: username=@%s id=%s", me.username, me.id)


def main() -> None:
app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()


app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("debug", cmd_debug))
app.add_handler(CommandHandler("whoami", cmd_whoami))


app.add_handler(CallbackQueryHandler(on_callback))


app.add_handler(MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.ChatType.CHANNEL, handle_file_message))
app.add_handler(MessageHandler((filters.Document.ALL | filters.PHOTO) & filters.ChatType.CHANNEL, handle_file_channel))


app.run_webhook(
listen="0.0.0.0",
port=PORT,
url_path="/webhook",
webhook_url=WEBHOOK_URL or None,
drop_pending_updates=True,
)


if __name__ == "__main__":
main()