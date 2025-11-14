# main_web_v2.py
# --- Telegram webhook: подтверждение документов + кнопки, без лишних статусов ---
# Маршруты: /healthz, /webhook
# Логика:
#   document -> [✅ Подтвердить][❌ Отклонить]
#   по Подтвердить -> processor.gpt_process() -> ST00012 -> QR -> пояснение
#   под результатом -> [💳 Оплатить][📥 Забрать][✖ Отмена]

import os
import io
import json
import uuid
import logging
from typing import Any, Dict, Optional

from flask import Flask, request, jsonify
from urllib.parse import urlencode
import urllib.request

from processor import gpt_process, build_st00012, make_qr_png

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("invoice-bot")

# -------- ENV --------
def _env() -> Dict[str, str]:
    bot_token = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip()    # например: https://<service>.onrender.com/webhook
    allowed_chat_id = (os.getenv("ALLOWED_CHAT_ID") or "").strip()
    allowed_topic_id = (os.getenv("ALLOWED_TOPIC_ID") or "").strip()
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN (или TELEGRAM_BOT_TOKEN).")
    if not openai_key:
        raise RuntimeError("Не задан OPENAI_API_KEY.")
    return {
        "BOT_TOKEN": bot_token,
        "WEBHOOK_URL": webhook_url,
        "ALLOWED_CHAT_ID": allowed_chat_id,
        "ALLOWED_TOPIC_ID": allowed_topic_id,
        "OPENAI_API_KEY": openai_key,
    }

ENV = _env()

# -------- TG API --------
def tg_api(method: str, data: Dict[str, Any]) -> Dict[str, Any]:
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

def tg_upload_doc(file_name: str, blob: bytes, data: Dict[str, Any]) -> Dict[str, Any]:
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    parts = []
    def add_field(k,v):
        parts.append(f"--{boundary}")
        parts.append(f'Content-Disposition: form-data; name="{k}"')
        parts.append("")
        parts.append(str(v))
    for k,v in data.items():
        add_field(k, v)
    parts.append(f"--{boundary}")
    parts.append(f'Content-Disposition: form-data; name="document"; filename="{file_name}"')
    parts.append("Content-Type: application/octet-stream")
    parts.append("")
    body = b""
    for p in parts:
        body += (p if isinstance(p, bytes) else p.encode("utf-8")) + b"\r\n"
    body += blob + b"\r\n"
    body += f"--{boundary}--".encode("utf-8") + b"\r\n"

    url = f"https://api.telegram.org/bot{ENV['BOT_TOKEN']}/sendDocument"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        log.error("TG API invalid JSON (upload): %s", raw)
        return {"ok": False, "raw": raw}

def get_file(file_id: str) -> bytes:
    info = tg_api("getFile", {"file_id": file_id})
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{ENV['BOT_TOKEN']}/{file_path}"
    with urllib.request.urlopen(url, timeout=60) as f:
        return f.read()

def send_text(chat_id: int, text: str, thread_id: Optional[int] = None,
              reply_to: Optional[int] = None, reply_markup: Optional[Dict[str, Any]] = None,
              parse_mode: Optional[str] = "Markdown"):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    if thread_id: payload["message_thread_id"] = thread_id
    if reply_to: payload["reply_to_message_id"] = reply_to
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    tg_api("sendMessage", payload)

def send_doc(chat_id: int, name: str, blob: bytes, caption: str = "", thread_id: Optional[int] = None):
    data = {"chat_id": chat_id}
    if thread_id: data["message_thread_id"] = thread_id
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "Markdown"
    tg_upload_doc(name, blob, data)

# -------- Кнопки --------
def kb_confirm(token: str) -> Dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "✅ Подтвердить", "callback_data": f"ok:{token}"},
        {"text": "❌ Отклонить", "callback_data": f"no:{token}"},
    ]]}

def kb_after(token: str) -> Dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "💳 Оплатить", "callback_data": f"pay:{token}"},
        {"text": "📥 Забрать", "callback_data": f"get:{token}"},
        {"text": "✖ Отмена", "callback_data": f"cancel:{token}"},
    ]]}

# -------- Память сессий --------
PENDING: Dict[str, Dict[str, Any]] = {}
RESULTS: Dict[str, Dict[str, Any]] = {}

# -------- Фильтры --------
def passes(msg: Dict[str, Any]) -> bool:
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    topic_id = msg.get("message_thread_id")
    topic_id = str(topic_id) if topic_id is not None else ""
    if ENV["ALLOWED_CHAT_ID"] and chat_id != ENV["ALLOWED_CHAT_ID"]:
        return False
    if ENV["ALLOWED_TOPIC_ID"] and topic_id != ENV["ALLOWED_TOPIC_ID"]:
        return False
    return True

# -------- Обработка --------
def on_confirm(ctx: Dict[str, Any]):
    chat_id = ctx["chat_id"]
    thread_id = ctx.get("thread_id")
    file_id = ctx["file_id"]
    file_name = ctx["file_name"]
    token = ctx["token"]

    try:
        blob = get_file(file_id)
    except Exception as e:
        send_text(chat_id, f"⚠️ Не удалось скачать файл: {e}", thread_id)
        return

    # GPT-обработка + нормализация внутри processing.gpt_process
    ok, fields, human_note = gpt_process(blob, file_name)
    if not ok:
        # human_note уже содержит подсказку «почему»
        send_text(chat_id, human_note, thread_id, parse_mode=None)
        return

    st = build_st00012(fields)
    qr = make_qr_png(st)

    RESULTS[token] = {"fields": fields, "st": st, "qr": qr, "file_name": file_name}

    caption = (
        "*Платёжный QR сформирован (ST00012).* \n\n"
        f"*ИНН:* `{fields.get('PayeeINN','-')}`\n"
        f"*КПП:* `{fields.get('KPP','-')}`\n"
        f"*Банк:* {fields.get('BankName','-')}\n"
        f"*БИК:* `{fields.get('BIC','-')}`\n"
        f"*К/с:* `{fields.get('CorrespAcc','-')}`\n"
        f"*Р/с:* `{fields.get('PersonalAcc','-')}`\n"
        f"*Сумма:* `{fields['Sum']/100:.2f} ₽`\n"
        f"*Назначение:* {fields.get('Purpose','-')}\n"
    )
    send_doc(chat_id, "qr.png", qr, caption=caption, thread_id=thread_id)
    send_text(chat_id, "Выберите действие 👇", thread_id, reply_markup=kb_after(token))

def handle_callback(cq: Dict[str, Any]):
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = int(chat.get("id"))
    thread_id = msg.get("message_thread_id")

    if data.startswith("ok:"):
        token = data.split(":",1)[1]
        ctx = PENDING.pop(token, None)
        if ctx:
            on_confirm(ctx)
        return
    if data.startswith("no:"):
        token = data.split(":",1)[1]
        PENDING.pop(token, None)
        send_text(chat_id, "❌ Обработка отменена.", thread_id)
        return
    if data.startswith("pay:"):
        token = data.split(":",1)[1]
        res = RESULTS.get(token)
        if res:
            f = res["fields"]
            txt = (
                "💳 *Оплата*\n\n"
                f"Получатель: {f.get('Name')}\n"
                f"ИНН: {f.get('PayeeINN')}\n"
                f"КПП: {f.get('KPP')}\n"
                f"Банк: {f.get('BankName')}\n"
                f"БИК: {f.get('BIC')}\n"
                f"К/с: {f.get('CorrespAcc')}\n"
                f"Р/с: {f.get('PersonalAcc')}\n"
                f"Сумма: {f['Sum']/100:.2f} ₽\n"
                f"Назначение: {f.get('Purpose')}\n"
            )
            send_text(chat_id, txt, thread_id)
        return
    if data.startswith("get:"):
        token = data.split(":",1)[1]
        res = RESULTS.get(token)
        if res:
            tg_upload_doc("payment_st00012.txt", res["st"].encode("utf-8"), {
                "chat_id": chat_id,
                "message_thread_id": thread_id or ""
            })
            send_doc(chat_id, "qr.png", res["qr"], caption="QR повторно", thread_id=thread_id)
        return
    if data.startswith("cancel:"):
        token = data.split(":",1)[1]
        RESULTS.pop(token, None)
        send_text(chat_id, "✖ Сессию результата очистил.", thread_id)
        return

def handle_update(update: Dict[str, Any]):
    cq = update.get("callback_query")
    if cq:
        handle_callback(cq)
        return
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg or not passes(msg):
        return
    chat = msg["chat"]; chat_id = int(chat["id"])
    thread_id = msg.get("message_thread_id")
    mid = msg.get("message_id")

    if "document" in msg:
        doc = msg["document"]
        token = uuid.uuid4().hex[:24]
        PENDING[token] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "file_id": doc["file_id"],
            "file_name": doc.get("file_name","document"),
            "token": token,
        }
        send_text(chat_id, f"Получен файл: *{PENDING[token]['file_name']}*\n\nПодтвердить обработку?",
                  thread_id, reply_to=mid, reply_markup=kb_confirm(token))
        return

    if "text" in msg:
        send_text(chat_id, "👋 Отправьте PDF/DOCX *файлом* (скрепкой). После загрузки появятся кнопки подтверждения.", thread_id)

# -------- Flask --------
app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        return "ok", 200
    update = request.get_json(force=True, silent=True) or {}
    try:
        handle_update(update)
    except Exception:
        log.exception("handler failed")
        return jsonify({"ok": False}), 500
    return jsonify({"ok": True}), 200

def main():
    port = int(os.getenv("PORT","10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
