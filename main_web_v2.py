# main_web_v2.py
# --- Telegram webhook + подтверждение документов кнопками ---
# Маршруты: /healthz (200 ok), /webhook и /webhook/<secret>
# Поток:
#  - document -> inline-кнопки [Подтвердить/Отклонить]
#  - callback_query "ok:<token>" -> process_document(...) -> отправка QR + пояснение
#  - callback_query "no:<token>" -> отмена
# Фильтры: ALLOWED_CHAT_ID / ALLOWED_TOPIC_ID (если заданы)

import os
import io
import json
import uuid
import logging
from typing import Any, Dict, Optional

from flask import Flask, request, abort, jsonify
from urllib.parse import urlencode
import urllib.request

# Для демо-QR (можно заменить своей реализацией):
# pip install qrcode[pil] pillow
import qrcode

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("tg-webhook")

# ------------------- ENV -------------------
def get_env() -> Dict[str, str]:
    bot_token = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip()
    webhook_secret = (os.getenv("WEBHOOK_SECRET") or "").strip()
    allowed_chat_id = (os.getenv("ALLOWED_CHAT_ID") or "").strip()
    allowed_topic_id = (os.getenv("ALLOWED_TOPIC_ID") or "").strip()
    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN (или TELEGRAM_BOT_TOKEN).")

    return {
        "BOT_TOKEN": bot_token,
        "WEBHOOK_URL": webhook_url,
        "WEBHOOK_SECRET": webhook_secret,
        "ALLOWED_CHAT_ID": allowed_chat_id,
        "ALLOWED_TOPIC_ID": allowed_topic_id,
    }

ENV = get_env()

# Хранилище «ожидающих подтверждения» документов (in-memory)
PENDING: Dict[str, Dict[str, Any]] = {}

# ------------------- TG API -------------------
def tg_api_request(method: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{ENV['BOT_TOKEN']}/{method}"
    body = urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        log.error("TG API invalid JSON: %s", raw)
        return {"ok": False, "raw": raw}

def tg_api_upload(method: str, files: Dict[str, bytes], data: Dict[str, Any]) -> Dict[str, Any]:
    # Простейший multipart/form-data (без внешних либ)
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    lines = []

    for k, v in data.items():
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{k}"')
        lines.append("")
        lines.append(str(v))

    for fname, blob in files.items():
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="document"; filename="{fname}"')
        lines.append("Content-Type: application/octet-stream")
        lines.append("")
        lines.append(blob)

    lines.append(f"--{boundary}--")
    # Склеиваем с корректной обработкой байтов
    body = b""
    for part in lines:
        if isinstance(part, bytes):
            body += part + b"\r\n"
        else:
            body += part.encode("utf-8") + b"\r\n"

    url = f"https://api.telegram.org/bot{ENV['BOT_TOKEN']}/{method}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        log.error("TG API invalid JSON (upload): %s", raw)
        return {"ok": False, "raw": raw}

def set_webhook_if_needed() -> None:
    if not ENV["WEBHOOK_URL"]:
        log.info("WEBHOOK_URL не задан — пропускаю установку вебхука (сервер поднят).")
        return
    if ENV["WEBHOOK_SECRET"]:
        if not ENV["WEBHOOK_URL"].rstrip("/").endswith(f"/{ENV['WEBHOOK_SECRET']}"):
            log.warning("WEBHOOK_SECRET задан, но WEBHOOK_URL не заканчивается на '/%s'", ENV["WEBHOOK_SECRET"])
    log.info("Настраиваю вебхук: %s", ENV["WEBHOOK_URL"])
    res = tg_api_request("setWebhook", {"url": ENV["WEBHOOK_URL"]})
    if not res.get("ok"):
        log.error("setWebhook error: %s", res)
    else:
        log.info("setWebhook ok")

def send_text(chat_id: int, text: str, thread_id: Optional[int] = None, reply_to: Optional[int] = None,
              reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if thread_id:
        payload["message_thread_id"] = thread_id
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    tg_api_request("sendMessage", payload)

def send_doc(chat_id: int, file_name: str, blob: bytes, caption: str = "", thread_id: Optional[int] = None) -> None:
    data = {"chat_id": chat_id}
    if thread_id:
        data["message_thread_id"] = thread_id
    if caption:
        data["caption"] = caption
    tg_api_upload("sendDocument", {file_name: blob}, data)

def get_file_bytes(file_id: str) -> bytes:
    # 1) getFile -> file_path
    res = tg_api_request("getFile", {"file_id": file_id})
    if not res.get("ok"):
        raise RuntimeError(f"getFile failed: {res}")
    file_path = res["result"]["file_path"]
    # 2) download
    url = f"https://api.telegram.org/file/bot{ENV['BOT_TOKEN']}/{file_path}"
    with urllib.request.urlopen(url, timeout=60) as f:
        return f.read()

# ------------------- ФИЛЬТРЫ -------------------
def passes_filters(msg: Dict[str, Any]) -> bool:
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    topic_id = msg.get("message_thread_id")
    topic_id_str = str(topic_id) if topic_id is not None else ""

    if ENV["ALLOWED_CHAT_ID"] and chat_id != ENV["ALLOWED_CHAT_ID"]:
        return False
    if ENV["ALLOWED_TOPIC_ID"] and topic_id_str != ENV["ALLOWED_TOPIC_ID"]:
        return False
    return True

# ------------------- ОБРАБОТКА -------------------
def build_inline_confirm(token: str) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Подтвердить", "callback_data": f"ok:{token}"},
                {"text": "❌ Отклонить", "callback_data": f"no:{token}"},
            ]
        ]
    }

def process_document(ctx: Dict[str, Any]) -> None:
    """
    Здесь встраивается твоя реальная обработка: извлечение реквизитов (GPT),
    генерация QR и отправка результата. Ниже — минимальная демонстрация:
    - скачиваем файл
    - формируем «псевдо-платёжную строку»
    - генерируем QR-картинку (PNG)
    - отправляем её назад с пояснением
    ЗАМЕНИ внутренности на свою реализацию, если нужно.
    """
    chat_id = ctx["chat_id"]
    thread_id = ctx.get("thread_id")
    file_id = ctx["file_id"]
    file_name = ctx["file_name"]

    try:
        file_bytes = get_file_bytes(file_id)
    except Exception as e:
        log.exception("Download failed")
        send_text(chat_id, f"⚠️ Не удалось скачать файл: {e}", thread_id)
        return

    # TODO: ВСТАВЬ СВОЙ ПАЙПЛАЙН:
    # 1) fields = extract_invoice_fields(file_bytes)  # ИНН, КПП, р/с, банк, сумма, назначение и пр.
    # 2) payload = build_payment_string(fields)       # строка по твоим правилам (ST00012/СБП/иное)
    # 3) qr_png = make_qr_png(payload)
    # Ниже — демонстрационный payload (просто чтобы проверить цепочку кнопки → ответ с QR):
    payload = f"INVOICE:{file_name}"
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_png = buf.getvalue()

    caption = (
        "🧾 Обработка завершена (демо).\n\n"
        "• Исходный файл: {fn}\n"
        "• Payload в QR: \"{pl}\"\n\n"
        "Заменю демо-процесс на твой пайплайн (GPT→реквизиты→QR) — скажи, и подключу функции."
    ).format(fn=file_name, pl=payload)

    send_doc(chat_id, "qr.png", qr_png, caption=caption, thread_id=thread_id)

def handle_update(update: Dict[str, Any]) -> None:
    # 1) callback_query (нажатие на кнопки)
    cq = update.get("callback_query")
    if cq:
        data = cq.get("data") or ""
        from_user = cq.get("from") or {}
        user_id = str(from_user.get("id") or "")
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {})
        chat_id = int(chat.get("id"))
        thread_id = msg.get("message_thread_id")

        if data.startswith("ok:"):
            token = data.split(":", 1)[1]
            ctx = PENDING.pop(token, None)
            if not ctx:
                send_text(chat_id, "⛔ Истекла сессия подтверждения. Отправь файл заново.", thread_id)
                return
            send_text(chat_id, "✅ Подтверждено. Запускаю обработку…", thread_id)
            process_document(ctx)
            return

        elif data.startswith("no:"):
            token = data.split(":", 1)[1]
            PENDING.pop(token, None)
            send_text(chat_id, "❌ Обработка отменена.", thread_id)
            return

        else:
            send_text(chat_id, "🤔 Неизвестная команда.", thread_id)
            return

    # 2) обычное сообщение (document / text)
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return
    if not passes_filters(message):
        return

    chat = message["chat"]
    chat_id = int(chat["id"])
    thread_id = message.get("message_thread_id")
    message_id = message.get("message_id")

    if "document" in message:
        doc = message["document"]
        file_id = doc["file_id"]
        file_name = doc.get("file_name", "document")

        # Регистрируем ожидаемую обработку и рисуем кнопки
        token = uuid.uuid4().hex[:24]
        PENDING[token] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "file_id": file_id,
            "file_name": file_name,
            "message_id": message_id,
        }

        kb = build_inline_confirm(token)
        send_text(
            chat_id,
            f"Получен файл: *{file_name}*\n\nПодтвердить обработку?",
            thread_id,
            reply_to=message_id,
            reply_markup=kb
        )
        return

    if "text" in message:
        send_text(
            chat_id,
            "👋 Отправьте PDF/DOCX *файлом* (скрепкой). После загрузки появятся кнопки подтверждения.",
            thread_id
        )
        return

# ------------------- Flask app -------------------
app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.route("/webhook", methods=["GET", "POST"])
@app.route("/webhook/<secret>", methods=["GET", "POST"])
def webhook(secret: Optional[str] = None):
    # секьюрность по секрету (если задан)
    if ENV["WEBHOOK_SECRET"]:
        if secret is None or secret != ENV["WEBHOOK_SECRET"]:
            return "forbidden", 403

    if request.method == "GET":
        return "ok", 200

    try:
        update = request.get_json(force=True, silent=True) or {}
    except Exception:
        log.exception("bad json")
        return jsonify({"ok": False, "error": "invalid json"}), 400

    try:
        handle_update(update)
    except Exception:
        log.exception("handler failed")
        return jsonify({"ok": False}), 500

    return jsonify({"ok": True}), 200

def main():
    set_webhook_if_needed()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
