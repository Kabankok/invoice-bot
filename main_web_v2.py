# main_web_v2.py
# --- Telegram webhook + GPT обработка счетов (устойчивый JSON-парсер) ---
# Поток:
#   1) документ -> [Подтвердить/Отклонить]
#   2) "Подтвердить" -> GPT -> JSON с реквизитами
#   3) ST00012 -> QR -> пояснение -> [Оплатить/Забрать/Отмена]
#
# Особенности:
#   - Промпт просит "строго JSON без обрамления", но защитно снимаем ```json ... ```
#   - Находим первый валидный JSON-объект в ответе, игнорируем лишний текст
#   - Принимаем лишние поля (VAT, VAT_Sum) без ошибок
#   - Сумму приводим к копейкам (int), даже если пришла строкой или в рублях

import os
import io
import re
import json
import uuid
import logging
from typing import Any, Dict, Optional, Tuple

from flask import Flask, request, jsonify
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
    parts = []

    def add_field(name: str, value: str):
        parts.append(f"--{boundary}")
        parts.append(f'Content-Disposition: form-data; name="{name}"')
        parts.append("")
        parts.append(value)

    for k, v in data.items():
        add_field(k, str(v))

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

def get_file_bytes(file_id: str) -> Tuple[bytes, str]:
    info = tg_api_request("getFile", {"file_id": file_id})
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{ENV['BOT_TOKEN']}/{file_path}"
    with urllib.request.urlopen(url, timeout=60) as f:
        return f.read(), (file_path.lower())

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

# ------------------- PROMPT -------------------
PROMPT = (
    "Ты — финансовый помощник. Тебе дают ТЕКСТ счёта (уже распознанный/извлечённый). "
    "Найди поля и верни СТРОГО ЧИСТЫЙ JSON, БЕЗ пояснений и без обрамления ```json. "
    "Ключи:\n"
    "{\n"
    ' "Name": "Наименование получателя",\n'
    ' "PersonalAcc": "Расчётный счёт (20 цифр)",\n'
    ' "BankName": "Банк получателя",\n'
    ' "BIC": "БИК (9 цифр)",\n'
    ' "CorrespAcc": "Корреспондентский счёт (20 цифр) или пусто",\n'
    ' "PayeeINN": "ИНН (10 или 12 цифр)",\n'
    ' "KPP": "КПП (9 цифр) или пусто",\n'
    ' "Sum": "Сумма в КОПЕЙКАХ (целое число)",\n'
    ' "Purpose": "Назначение платежа: явное из счёта, иначе составь \'Оплата по счёту №... от ...\'. Если НДС не указан — добавь \'Без НДС\'."\n'
    "}\n"
    "Только JSON-объект. Никаких комментариев, текста, Markdown."
)

# ------------------- JSON-парсер (устойчивый) -------------------
def _strip_code_fences(s: str) -> str:
    # Снимаем ```json ... ``` или ``` ... ```
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()

def _find_first_json_object(s: str) -> Optional[str]:
    """
    Находит первый валидно сбалансированный {...} объект верхнего уровня.
    Учитываем вложенные { } и строки.
    """
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "\"":
                in_str = False
        else:
            if ch == "\"":
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i+1]
    return None

def parse_gpt_json(raw: str) -> Dict[str, Any]:
    """
    Делает всё возможное: снимает фенсы, выдёргивает первый JSON-объект, парсит.
    Если не получилось — вернёт {}.
    """
    if not raw:
        return {}
    s = _strip_code_fences(raw)
    # иногда модель всё равно добавляет текст до/после — вырежем чистый объект
    obj = _find_first_json_object(s) or s
    try:
        return json.loads(obj)
    except Exception:
        log.error("parse_gpt_json failed. raw: %s", raw)
        return {}

# ------------------- GPT вызов -------------------
def gpt_extract_fields(text: str) -> Dict[str, Any]:
    # Страхуемся от слишком длинного бинарного контента: оставим разумный предел
    snippet = text if len(text) <= 32000 else text[:32000]
    resp = GPT.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": snippet}
        ],
        temperature=0
    )
    content = resp.choices[0].message.content.strip() if resp.choices else ""
    if not content:
        return {}
    # Логируем укороченную версию для отладки
    log.debug("GPT raw content (trimmed): %s", content[:1000])
    return parse_gpt_json(content)

# ------------------- Нормализация полей -------------------
REQ_KEYS = ["Name","PersonalAcc","BankName","BIC","CorrespAcc","PayeeINN","KPP","Sum","Purpose"]

def to_kop(any_sum: Any) -> int:
    """
    Приводим сумму к копейкам (int).
    Допускаем: int в копейках, float/строку в рублях (с точкой/запятой), строку в копейках.
    """
    if any_sum is None:
        return 0
    # уже целое — считаем, что это копейки
    if isinstance(any_sum, int):
        return max(any_sum, 0)
    s = str(any_sum).strip().replace(" ", "").replace("\u00A0", "")
    # если похоже на рубли с разделителем — переводим в копейки
    if "," in s or "." in s:
        sep = "," if "," in s else "."
        rub, kop = s.split(sep, 1)
        rub = re.sub(r"\D", "", rub or "0")
        kop = re.sub(r"\D", "", kop or "0")[:2].ljust(2, "0")
        try:
            return max(int(rub) * 100 + int(kop), 0)
        except Exception:
            return 0
    # иначе пусть это целое число в копейках
    s_digits = re.sub(r"\D", "", s)
    try:
        return max(int(s_digits or "0"), 0)
    except Exception:
        return 0

def normalize_fields(f: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: (f.get(k) or "") for k in REQ_KEYS}
    out["Sum"] = to_kop(f.get("Sum"))
    # Назначение: добавим "Без НДС", если модель не указала
    purpose = str(out.get("Purpose") or "").strip()
    if purpose and ("НДС" not in purpose.upper()):
        purpose = f"{purpose}; Без НДС"
    if not purpose:
        purpose = "Оплата по счёту; Без НДС"
    out["Purpose"] = purpose
    # Строковые поля — чистим пробелы/переводы строк и запрещённые символы "|"
    for key in ["Name","PersonalAcc","BankName","BIC","CorrespAcc","PayeeINN","KPP","Purpose"]:
        v = str(out.get(key, "")).replace("|", " ").replace("\r", " ").replace("\n", " ").strip()
        out[key] = v
    return out

# ------------------- ST00012 / QR -------------------
def build_st00012(fields: Dict[str, Any]) -> str:
    parts = ["ST00012"]
    def add(k, v):
        if v:
            parts.append(f"{k}={v}")
    add("Name", fields.get("Name"))
    add("PersonalAcc", fields.get("PersonalAcc"))
    add("BankName", fields.get("BankName"))
    add("BIC", fields.get("BIC"))
    add("CorrespAcc", fields.get("CorrespAcc"))
    add("PayeeINN", fields.get("PayeeINN"))
    add("KPP", fields.get("KPP"))
    if fields.get("Sum"):
        add("Sum", str(fields["Sum"]))
    add("Purpose", fields.get("Purpose"))
    return "|".join(parts)

def make_qr(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ------------------- Основной поток -------------------
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

def process_document(ctx: Dict[str, Any]) -> None:
    chat_id = ctx["chat_id"]
    thread_id = ctx.get("thread_id")
    file_id = ctx["file_id"]
    file_name = ctx["file_name"]
    token = ctx["token"]

    # 1) скачиваем файл
    try:
        file_bytes, file_path = get_file_bytes(file_id)
    except Exception as e:
        send_text(chat_id, f"⚠️ Не удалось скачать файл: {e}", thread_id)
        return

    # 2) Пытаемся вытащить человекочитаемый текст для GPT.
    #    (Если это бинарь/PDF — иногда там уже текст; если нет, всё равно попробуем.)
    try:
        text = file_bytes.decode("utf-8", errors="ignore")
    except Exception:
        text = ""

    if not text or len(text.strip()) < 30:
        # Если текста мало, всё равно отправим то, что есть — часто хватает.
        # В случае совсем пустых сканов без OCR — нужно будет подключить OCR.
        pass

    # 3) GPT -> поля
    fields_raw = gpt_extract_fields(text)
    if not fields_raw:
        send_text(chat_id, "⚠️ GPT вернул пустой ответ. Проверьте счёт.", thread_id)
        return

    fields = normalize_fields(fields_raw)

    # Проверим ключевые поля
    missing = []
    for k in ["PersonalAcc", "BIC", "PayeeINN", "Sum"]:
        if not fields.get(k):
            missing.append(k)
    if missing:
        send_text(chat_id, "⚠️ Не хватает данных для платёжного QR: " + ", ".join(missing), thread_id)
        # дадим отладочный дамп
        dbg = json.dumps(fields, ensure_ascii=False, indent=2)
        send_text(chat_id, f"🔎 Распознано:\n```\n{dbg}\n```", thread_id, parse_mode=None)
        return

    # 4) ST00012 + QR
    st = build_st00012(fields)
    qr_png = make_qr(st)

    RESULTS[token] = {
        "fields": fields,
        "st": st,
        "qr_png": qr_png,
        "file_name": file_name,
    }

    # 5) Ответ
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
    send_doc(chat_id, "qr.png", qr_png, caption=caption, thread_id=thread_id)
    send_text(chat_id, "Выберите действие 👇", thread_id, reply_markup=kb_after_result(token))

# ------------------- Callbacks -------------------
def handle_callback(cq: Dict[str, Any]) -> None:
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat = (msg.get("chat") or {})
    chat_id = int(chat.get("id"))
    thread_id = msg.get("message_thread_id")

    if data.startswith("ok:"):
        token = data.split(":", 1)[1]
        ctx = PENDING.pop(token, None)
        if not ctx:
            send_text(chat_id, "⛔ Сессия подтверждения не найдена. Отправьте файл заново.", thread_id)
            return
        # без лишнего промежуточного сообщения — сразу в обработку
        process_document(ctx)
        return

    if data.startswith("no:"):
        token = data.split(":", 1)[1]
        PENDING.pop(token, None)
        send_text(chat_id, "❌ Обработка отменена.", thread_id)
        return

    if data.startswith("pay:"):
        token = data.split(":", 1)[1]
        res = RESULTS.get(token)
        if not res:
            send_text(chat_id, "⛔ Нет данных платежа. Сформируйте QR заново.", thread_id)
            return
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
            f"Сумма: {f['Sum']/100:.2f} ₽\n"
            f"Назначение: {f.get('Purpose')}\n"
        )
        send_text(chat_id, msg_text, thread_id)
        return

    if data.startswith("get:"):
        token = data.split(":", 1)[1]
        res = RESULTS.get(token)
        if not res:
            send_text(chat_id, "⛔ Нет данных для выдачи. Сформируйте QR заново.", thread_id)
            return
        st = res["st"]
        qr_png = res["qr_png"]
        tg_api_upload_document("payment_st00012.txt", st.encode("utf-8"), {
            "chat_id": chat_id,
            "message_thread_id": thread_id or ""
        })
        send_doc(chat_id, "qr.png", qr_png, caption="QR повторно", thread_id=thread_id)
        return

    if data.startswith("cancel:"):
        token = data.split(":", 1)[1]
        RESULTS.pop(token, None)
        send_text(chat_id, "✖ Готово. Сессию результата очистил.", thread_id)
        return

    send_text(chat_id, "🤔 Неизвестная команда.", thread_id)

# ------------------- Update handler -------------------
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

def handle_update(update: Dict[str, Any]) -> None:
    cq = update.get("callback_query")
    if cq:
        handle_callback(cq)
        return

    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return
    if not passes_filters(msg):
        return

    chat = msg["chat"]
    chat_id = int(chat["id"])
    thread_id = msg.get("message_thread_id")
    mid = msg.get("message_id")

    if "document" in msg:
        doc = msg["document"]
        file_id = doc["file_id"]
        file_name = doc.get("file_name", "document")
        token = uuid.uuid4().hex[:24]
        PENDING[token] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "file_id": file_id,
            "file_name": file_name,
            "token": token,
        }
        send_text(
            chat_id,
            f"Получен файл: *{file_name}*\n\nПодтвердить обработку?",
            thread_id,
            reply_to=mid,
            reply_markup=kb_confirm(token)
        )
        return

    if "text" in msg:
        send_text(chat_id, "👋 Отправьте PDF/DOCX *файлом* (скрепкой). После загрузки появятся кнопки подтверждения.", thread_id)

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
    try:
        handle_update(update)
    except Exception:
        log.exception("handler failed")
        return jsonify({"ok": False}), 500
    return jsonify({"ok": True}), 200

def main():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
