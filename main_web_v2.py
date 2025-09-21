# main_web_v2.py
# --- Telegram webhook + GPT обработка счетов ---
# Поток:
#   1. Получаем документ -> кнопки [Подтвердить/Отклонить]
#   2. Нажали Подтвердить -> GPT извлекает реквизиты JSON-форматом
#   3. Python собирает ST00012, генерирует QR
#   4. Отправляем QR + пояснение + кнопки [Оплатить][Забрать][Отмена]

import os
import io
import json
import uuid
import logging
from typing import Any, Dict, Optional

from flask import Flask, request, abort, jsonify
from urllib.parse import urlencode
import urllib.request

import qrcode
from openai import OpenAI

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("invoice-bot")

# ------------------- ENV -------------------
def get_env() -> Dict[str, str]:
    bot_token = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip()
    webhook_secret = (os.getenv("WEBHOOK_SECRET") or "").strip()
    allowed_chat_id = (os.getenv("ALLOWED_CHAT_ID") or "").strip()
    allowed_topic_id = (os.getenv("ALLOWED_TOPIC_ID") or "").strip()
    openai_api_key = (os.getenv("OPENAI_API_KEY") or "").strip()

    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN (или TELEGRAM_BOT_TOKEN).")
    if not openai_api_key:
        raise RuntimeError("Не задан OPENAI_API_KEY.")

    return {
        "BOT_TOKEN": bot_token,
        "WEBHOOK_URL": webhook_url,
        "WEBHOOK_SECRET": webhook_secret,
        "ALLOWED_CHAT_ID": allowed_chat_id,
        "ALLOWED_TOPIC_ID": allowed_topic_id,
        "OPENAI_API_KEY": openai_api_key,
    }

ENV = get_env()
GPT = OpenAI(api_key=ENV["OPENAI_API_KEY"])

# ------------------- Хранилища -------------------
PENDING: Dict[str, Dict[str, Any]] = {}
RESULTS: Dict[str, Dict[str, Any]] = {}

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

def tg_api_upload_document(file_name: str, blob: bytes, data: Dict[str, Any]) -> Dict[str, Any]:
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    lines = []

    for k, v in data.items():
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{k}"')
        lines.append("")
        lines.append(str(v))

    lines.append(f"--{boundary}")
    lines.append(f'Content-Disposition: form-data; name="document"; filename="{file_name}"')
    lines.append("Content-Type: application/octet-stream")
    lines.append("")
    lines.append(blob)

    lines.append(f"--{boundary}--")

    body = b""
    for part in lines:
        if isinstance(part, bytes):
            body += part + b"\r\n"
        else:
            body += part.encode("utf-8") + b"\r\n"

    url = f"https://api.telegram.org/bot{ENV['BOT_TOKEN']}/sendDocument"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        log.error("TG API invalid JSON (upload): %s", raw)
        return {"ok": False, "raw": raw}

def get_file_bytes(file_id: str) -> bytes:
    info = tg_api_request("getFile", {"file_id": file_id})
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{ENV['BOT_TOKEN']}/{file_path}"
    with urllib.request.urlopen(url, timeout=60) as f:
        return f.read()

def send_text(chat_id: int, text: str, thread_id: Optional[int] = None,
              reply_to: Optional[int] = None, reply_markup: Optional[Dict[str, Any]] = None,
              parse_mode: Optional[str] = "Markdown") -> None:
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
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
        data["parse_mode"] = "Markdown"
    tg_api_upload_document(file_name, blob, data)

# ------------------- Кнопки -------------------
def kb_confirm(token: str) -> Dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "✅ Подтвердить", "callback_data": f"ok:{token}"},
        {"text": "❌ Отклонить", "callback_data": f"no:{token}"},
    ]]}

def kb_after_result(token: str) -> Dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "💳 Оплатить", "callback_data": f"pay:{token}"},
        {"text": "📥 Забрать", "callback_data": f"get:{token}"},
        {"text": "✖ Отмена", "callback_data": f"cancel:{token}"},
    ]]}

# ------------------- GPT обработка -------------------
PROMPT = """
Ты — финансовый помощник. Тебе дают текст счёта (PDF/DOCX), твоя задача:
1. Найти ИНН, КПП, расчётный счёт, корр. счёт, БИК, банк.
2. Найти сумму (в рублях).
3. Определить назначение платежа: если в счёте оно указано — взять его; если нет — составь "Оплата по счёту №… от …".
4. Если в счёте есть НДС — укажи "НДС X%" и сумму; если нет — напиши "Без НДС".
Ответ строго JSON-форматом с ключами:
{
 "Name": "...",
 "PersonalAcc": "...",
 "BankName": "...",
 "BIC": "...",
 "CorrespAcc": "...",
 "PayeeINN": "...",
 "KPP": "...",
 "Sum": число_в_копейках,
 "Purpose": "..."
}
"""

def gpt_extract_fields(text: str) -> Dict[str, Any]:
    completion = GPT.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text}
        ],
        temperature=0
    )
    raw = completion.choices[0].message.content.strip()
    try:
        return json.loads(raw)
    except Exception:
        log.error("GPT вернул не-JSON: %s", raw)
        return {}

# ------------------- Обработка документа -------------------
def make_qr(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def process_document(ctx: Dict[str, Any]) -> None:
    chat_id = ctx["chat_id"]
    thread_id = ctx.get("thread_id")
    file_id = ctx["file_id"]
    file_name = ctx["file_name"]
    token = ctx["token"]

    try:
        file_bytes = get_file_bytes(file_id)
    except Exception as e:
        send_text(chat_id, f"⚠️ Не удалось скачать файл: {e}", thread_id)
        return

    # отправим файл текстом в GPT (декодируем)
    try:
        text = file_bytes.decode("utf-8", errors="ignore")
    except Exception:
        text = ""

    fields = gpt_extract_fields(text)
    if not fields or not fields.get("PersonalAcc") or not fields.get("BIC"):
        send_text(chat_id, "⚠️ GPT не смог извлечь реквизиты. Проверьте счёт.", thread_id)
        return

    # Формируем ST00012
    parts = ["ST00012"]
    for k, v in fields.items():
        if v:
            parts.append(f"{k}={v}")
    st = "|".join(parts)
    qr_png = make_qr(st)

    RESULTS[token] = {"fields": fields, "st": st, "qr_png": qr_png, "file_name": file_name}

    caption = (
        "*Платёжный QR сформирован (ST00012).* \n\n"
        f"*ИНН:* `{fields.get('PayeeINN','-')}`\n"
        f"*КПП:* `{fields.get('KPP','-')}`\n"
        f"*Банк:* {fields.get('BankName','-')}\n"
        f"*БИК:* `{fields.get('BIC','-')}`\n"
        f"*К/с:* `{fields.get('CorrespAcc','-')}`\n"
        f"*Р/с:* `{fields.get('PersonalAcc','-')}`\n"
        f"*Сумма:* `{int(fields.get('Sum',0))/100:.2f} ₽`\n"
        f"*Назначение:* {fields.get('Purpose','-')}\n"
    )
    send_doc(chat_id, "qr.png", qr_png, caption=caption, thread_id=thread_id)
    send_text(chat_id, "Выберите действие 👇", thread_id, reply_markup=kb_after_result(token))

# ------------------- Callback -------------------
def handle_callback(cq: Dict[str, Any]) -> None:
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = int(msg.get("chat", {}).get("id"))
    thread_id = msg.get("message_thread_id")

    if data.startswith("ok:"):
        token = data.split(":", 1)[1]
        ctx = PENDING.pop(token, None)
        if ctx:
            process_document(ctx)
        return
    if data.startswith("no:"):
        send_text(chat_id, "❌ Обработка отменена.", thread_id)
        return
    if data.startswith("pay:"):
        token = data.split(":", 1)[1]
        res = RESULTS.get(token)
        if res:
            f = res["fields"]
            msg_text = (
                "💳 *Оплата*\n\n"
                f"Получатель: {f.get('Name')}\n"
                f"ИНН: {f.get('PayeeINN')}\n"
                f"КПП: {f.get('KPP')}\n"
                f"Банк: {f.get('BankName')}\n"
                f"БИК: {f.get('BIC')}\n"
                f"К/с: {f.get('CorrespAcc')}\n"
                f"Р/с: {f.get('PersonalAcc')}\n"
                f"Сумма: {int(f.get('Sum',0))/100:.2f} ₽\n"
                f"Назначение: {f.get('Purpose')}\n"
            )
            send_text(chat_id, msg_text, thread_id)
        return
    if data.startswith("get:"):
        token = data.split(":", 1)[1]
        res = RESULTS.get(token)
        if res:
            tg_api_upload_document("payment_st00012.txt", res["st"].encode(), {
                "chat_id": chat_id,
                "message_thread_id": thread_id or ""
            })
            send_doc(chat_id, "qr.png", res["qr_png"], caption="QR повторно", thread_id=thread_id)
        return
    if data.startswith("cancel:"):
        send_text(chat_id, "✖ Отменено.", thread_id)
        return

# ------------------- Update handler -------------------
def handle_update(update: Dict[str, Any]) -> None:
    cq = update.get("callback_query")
    if cq:
        handle_callback(cq)
        return

    msg = update.get("message") or {}
    if not msg:
        return
    chat_id = int(msg.get("chat", {}).get("id"))
    thread_id = msg.get("message_thread_id")
    mid = msg.get("message_id")

    if "document" in msg:
        doc = msg["document"]
        file_id = doc["file_id"]
        file_name = doc.get("file_name", "document")
        token = uuid.uuid4().hex[:24]
        PENDING[token] = {"chat_id": chat_id, "thread_id": thread_id,
                          "file_id": file_id, "file_name": file_name, "token": token}
        send_text(chat_id, f"Получен файл: *{file_name}*\n\nПодтвердить обработку?",
                  thread_id, reply_to=mid, reply_markup=kb_confirm(token))
        return

    if "text" in msg:
        send_text(chat_id, "👋 Отправьте PDF/DOCX файлом. Появятся кнопки подтверждения.", thread_id)

# ------------------- Flask app -------------------
app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.route("/webhook", methods=["GET", "POST"])
@app.route("/webhook/<secret>", methods=["GET", "POST"])
def webhook(secret: Optional[str] = None):
    if ENV["WEBHOOK_SECRET"]:
        if secret is None or secret != ENV["WEBHOOK_SECRET"]:
            return "forbidden", 403
    if request.method == "GET":
        return "ok", 200

    update = request.get_json(force=True, silent=True) or {}
    handle_update(update)
    return jsonify({"ok": True})

def main():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
