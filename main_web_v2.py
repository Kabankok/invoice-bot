# main_web_v2.py
# --- Telegram webhook с полным потоком обработки счетов ---
# Возможности:
#  - /healthz (200 ok)
#  - /webhook и /webhook/<secret> (универсально)
#  - document -> кнопки [✅ Подтвердить] [❌ Отклонить]
#  - по Подтвердить: извлечение реквизитов из PDF/DOCX/текста, сборка ST00012, генерация QR PNG
#  - отправка QR + подробное пояснение (ИНН/КПП/банк/р/с/назначение/НДС/сумма)
#  - кнопки под результатом: [💳 Оплатить] [📥 Забрать] [✖ Отмена]
#  - обработка callback_query: pay:<token>, get:<token>, cancel:<token>
#
# Примечания:
#  - Для PDF текста используется pdfminer.six (только "текстовые" PDF). Скан-изображения без OCR не прочитает.
#  - Для DOCX используется python-docx.
#  - Если нужные поля не найдены — возвращается подробная диагностика.
#  - Формат QR: ST00012 (банковский QR).
#  - VAT (НДС): если найден — добавляем в Purpose. Иначе "Без НДС".
#
# Требования: Flask, qrcode[pil], Pillow, pdfminer.six, python-docx

import os
import io
import re
import json
import uuid
import logging
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, Tuple

from flask import Flask, request, abort, jsonify
from urllib.parse import urlencode
import urllib.request

import qrcode

# --- для DOCX ---
try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None

# --- для PDF (только текстовые) ---
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None

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

# ------------------- МОДЕЛИ -------------------
@dataclass
class InvoiceFields:
    Name: str = ""          # Наименование получателя
    PersonalAcc: str = ""   # Р/с
    BankName: str = ""      # Наименование банка
    BIC: str = ""           # БИК
    CorrespAcc: str = ""    # К/с
    PayeeINN: str = ""      # ИНН
    KPP: str = ""           # КПП (если есть)
    Sum: int = 0            # сумма в копейках
    Purpose: str = ""       # Назначение платежа (с НДС/Без НДС)

    def to_st00012(self) -> str:
        # Формируем строку ST00012 (разделитель "|", ключ=значение)
        parts = ["ST00012"]
        def add(k, v): 
            if v:
                # Экранируем запрещённые символы по-хорошему — упрощённо убираем "|\n\r".
                v = str(v).replace("|", " ").replace("\n", " ").replace("\r", " ").strip()
                parts.append(f"{k}={v}")
        add("Name", self.Name)
        add("PersonalAcc", self.PersonalAcc)
        add("BankName", self.BankName)
        add("BIC", self.BIC)
        add("CorrespAcc", self.CorrespAcc)
        add("PayeeINN", self.PayeeINN)
        add("KPP", self.KPP)
        if self.Sum and self.Sum > 0:
            add("Sum", str(self.Sum))
        add("Purpose", self.Purpose or "Без НДС")
        return "|".join(parts)

# Хранилища сессий
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

def set_webhook_if_needed() -> None:
    if not ENV["WEBHOOK_URL"]:
        log.info("WEBHOOK_URL не задан — сервер поднят, но setWebhook пропущен.")
        return
    if ENV["WEBHOOK_SECRET"]:
        if not ENV["WEBHOOK_URL"].rstrip("/").endswith(f"/{ENV['WEBHOOK_SECRET']}"):
            log.warning("WEBHOOK_SECRET задан, но WEBHOOK_URL не заканчивается на '/%s'", ENV["WEBHOOK_SECRET"])
    res = tg_api_request("setWebhook", {"url": ENV["WEBHOOK_URL"]})
    log.info("setWebhook: %s", res)

def send_text(chat_id: int, text: str, thread_id: Optional[int] = None, reply_to: Optional[int] = None,
              reply_markup: Optional[Dict[str, Any]] = None, parse_mode: Optional[str] = "Markdown") -> None:
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

def get_file_bytes(file_id: str) -> Tuple[bytes, str]:
    info = tg_api_request("getFile", {"file_id": file_id})
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{ENV['BOT_TOKEN']}/{file_path}"
    with urllib.request.urlopen(url, timeout=60) as f:
        return f.read(), (file_path.lower())

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

# ------------------- ПАРСИНГ СЧЕТА -------------------
def bytes_to_text(file_bytes: bytes, file_path: str) -> str:
    if file_path.endswith(".docx") and DocxDocument:
        try:
            bio = io.BytesIO(file_bytes)
            doc = DocxDocument(bio)
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            log.exception("DOCX parse failed")
    if file_path.endswith(".pdf") and pdf_extract_text:
        try:
            bio = io.BytesIO(file_bytes)
            # pdfminer ожидает путь/файл; используем temp в памяти невозможно — поэтому сохранение временное
            # На Render FS доступен — но проще: pdfminer поддерживает file-like? В свежей версии high_level.extract_text поддерживает.
            text = pdf_extract_text(bio)  # может вернуть пусто для сканов
            return text or ""
        except Exception:
            log.exception("PDF parse failed")

    # fallback: пробуем как текст
    try:
        return file_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""

def extract_fields_from_text(text: str, default_name: str = "Получатель") -> InvoiceFields:
    # Удалим лишние пробелы
    t = re.sub(r"[ \t]+", " ", text)
    t = t.replace("\r", "")
    # Регексы
    inn = re.search(r"(?:ИНН|INN)\D*?(\d{10}|\d{12})", t, re.IGNORECASE)
    kpp = re.search(r"(?:КПП)\D*?(\d{9})", t, re.IGNORECASE)
    bic = re.search(r"(?:БИК)\D*?(\d{9})", t, re.IGNORECASE)
    rs = re.search(r"(?:р[./\s-]*с[./\s-]*|PersonalAcc|P\/?Acc)\D*?(\d{20})", t, re.IGNORECASE)
    ks = re.search(r"(?:к[./\s-]*с[./\s-]*|CorrespAcc|K\/?Acc)\D*?(\d{20})", t, re.IGNORECASE)
    bank = re.search(r"(?:Банк(?: получателя)?|Bank Name|Наименование банка)\D*?([A-Za-zА-Яа-я0-9\"«» .,-]{6,})", t)
    # Сумма: ищем "Итого", "К оплате", "Сумма" и т.п.
    sum_match = re.search(r"(?:Итого|К оплате|Сумма к оплате|Всего к оплате|Сумма)\D*?([\d\s]+[.,]\d{2})", t, re.IGNORECASE)
    # Назначение
    purpose = None
    # Явное назначение
    purpose_match = re.search(r"(?:Назначение платежа|Назначение|Основание|За что)\D*?[:\-–]\s*(.+)", t, re.IGNORECASE)
    if purpose_match:
        purpose = purpose_match.group(1).strip()
        # Обрежем по концу строки/двух переводов
        purpose = purpose.split("\n")[0].strip()

    # НДС
    vat = None
    vat_pct = None
    vat_match = re.search(r"(НДС)\s*(?:[:\-–]\s*)?(\d{1,2})\s*%?\s*(?:[,;]|$)", t, re.IGNORECASE)
    if vat_match:
        vat = "НДС"
        vat_pct = vat_match.group(2)

    # Сумма превращаем в копейки
    def money_to_kop(m: str) -> int:
        m = m.replace(" ", "").replace("\u00A0", "")
        m = m.replace("руб", "").replace("₽", "")
        m = m.strip()
        if "," in m:
            rub, kop = m.split(",", 1)
        elif "." in m:
            rub, kop = m.split(".", 1)
        else:
            rub, kop = m, "00"
        rub = re.sub(r"\D", "", rub)
        kop = re.sub(r"\D", "", kop)[:2].ljust(2, "0")
        if not rub:
            rub = "0"
        return int(rub) * 100 + int(kop)

    Sum = money_to_kop(sum_match.group(1)) if sum_match else 0

    # Если назначения нет — сделаем базовое правило
    # (как ты просил: явное назначение из счета, иначе номер/дата, иначе "Без НДС/счет <...>")
    if not purpose:
        # попробуем вытащить номер/дату
        num = re.search(r"(?:Сч[её]т(?:-фактура)?\s*№\s*|№\s*)([A-Za-z0-9\-_/]+)", t, re.IGNORECASE)
        dt = re.search(r"(\d{2}[./]\d{2}[./]\d{4})", t)
        if num and dt:
            purpose = f"Оплата по счёту №{num.group(1)} от {dt.group(1)}"
        elif num:
            purpose = f"Оплата по счёту №{num.group(1)}"
        else:
            purpose = "Оплата по счёту"

    # НДС маркировка
    if vat and vat_pct:
        purpose = f"{purpose}; НДС {vat_pct}%"
    else:
        # Если нигде не встретилось упоминание НДС — зафиксируем "Без НДС"
        if "НДС" not in purpose.upper():
            purpose = f"{purpose}; Без НДС"

    fields = InvoiceFields(
        Name=(bank.group(0).split(":")[0].strip() if False else default_name),  # имя получателя часто отдельно; оставим как "Получатель"
        PersonalAcc=rs.group(1) if rs else "",
        BankName=(bank.group(1).strip() if bank else ""),
        BIC=bic.group(1) if bic else "",
        CorrespAcc=ks.group(1) if ks else "",
        PayeeINN=inn.group(1) if inn else "",
        KPP=kpp.group(1) if kpp else "",
        Sum=Sum,
        Purpose=purpose.strip()
    )
    return fields

# ------------------- QR -------------------
def make_qr_png(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ------------------- КНОПКИ -------------------
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

# ------------------- ОСНОВНОЙ ПОТОК -------------------
def process_document(ctx: Dict[str, Any]) -> None:
    """
    1) Скачиваем файл
    2) Извлекаем текст (PDF/DOCX/utf-8)
    3) Парсим реквизиты -> InvoiceFields
    4) Собираем ST00012 -> QR PNG
    5) Отправляем PNG + подробное пояснение + кнопки [Оплатить][Забрать]
    """
    chat_id = ctx["chat_id"]
    thread_id = ctx.get("thread_id")
    file_id = ctx["file_id"]
    file_name = ctx["file_name"]
    token = ctx["token"]

    try:
        file_bytes, file_path = get_file_bytes(file_id)
    except Exception as e:
        log.exception("Download failed")
        send_text(chat_id, f"⚠️ Не удалось скачать файл: {e}", thread_id)
        return

    text = bytes_to_text(file_bytes, file_path)
    if not text or len(text) < 30:
        send_text(chat_id,
                  "⚠️ Не удалось извлечь текст из файла. Возможно, это отсканированный PDF без текста. "
                  "Нужен OCR — могу подключить позже (Tesseract/Cloud Vision).",
                  thread_id)
        return

    fields = extract_fields_from_text(text, default_name="Получатель")
    missing = []
    for k in ["PersonalAcc", "BIC", "PayeeINN", "Sum"]:
        if not getattr(fields, k):
            missing.append(k)
    if missing:
        send_text(chat_id,
                  "⚠️ Не хватает данных для платёжного QR: " + ", ".join(missing) +
                  ". Проверьте, что в счёте есть ИНН, БИК, р/с и сумма.",
                  thread_id)
        # всё равно покажем, что распознали
        send_text(chat_id, f"🔎 Распознано:\n```\n{json.dumps(asdict(fields), ensure_ascii=False, indent=2)}\n```",
                  thread_id, parse_mode=None)
        return

    st = fields.to_st00012()
    qr_png = make_qr_png(st)

    # Сохраним результат для дальнейших кнопок
    RESULTS[token] = {
        "fields": fields,
        "st": st,
        "qr_png": qr_png,
        "file_name": file_name,
    }

    caption = (
        "*Платёжный QR сформирован (ST00012).* \n\n"
        f"*ИНН:* `{fields.PayeeINN}`\n"
        f"*КПП:* `{fields.KPP or '-'}`\n"
        f"*Банк:* {fields.BankName or '-'}\n"
        f"*БИК:* `{fields.BIC}`\n"
        f"*К/с:* `{fields.CorrespAcc or '-'}`\n"
        f"*Р/с:* `{fields.PersonalAcc}`\n"
        f"*Сумма:* `{fields.Sum/100:.2f} ₽`\n"
        f"*Назначение:* {fields.Purpose}\n\n"
        "QR ниже. Кнопки под ним:"
    )
    send_doc(chat_id, "qr.png", qr_png, caption=caption, thread_id=thread_id)

    # Отдельным сообщением — кнопки действий
    send_text(chat_id, "Выберите действие 👇", thread_id, reply_markup=kb_after_result(token))

def handle_callback(cq: Dict[str, Any]) -> None:
    data = cq.get("data") or ""
    from_user = cq.get("from") or {}
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
        send_text(chat_id, "✅ Подтверждено. Запускаю обработку…", thread_id)
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
        fields: InvoiceFields = res["fields"]
        st = res["st"]
        # «Оплатить»: даём инструкции и саму строку ST00012 (её понимают банки при сканировании QR; вручную вставлять редко нужно)
        msg_text = (
            "💳 *Оплата*\n\n"
            "Сканируйте QR в приложении банка. "
            "Если требуется ручной ввод — используйте реквизиты ниже:\n\n"
            f"*Получатель:* {fields.Name or '—'}\n"
            f"*ИНН:* `{fields.PayeeINN}`\n"
            f"*КПП:* `{fields.KPP or '-'}\n"
            f"*Банк:* {fields.BankName or '-'}\n"
            f"*БИК:* `{fields.BIC}`\n"
            f"*К/с:* `{fields.CorrespAcc or '-'}\n"
            f"*Р/с:* `{fields.PersonalAcc}`\n"
            f"*Сумма:* `{fields.Sum/100:.2f} ₽`\n"
            f"*Назначение:* {fields.Purpose}\n\n"
            "_(Тех. строка ST00012 доступна по кнопке «Забрать».)_"
        )
        send_text(chat_id, msg_text, thread_id)
        return

    if data.startswith("get:"):
        token = data.split(":", 1)[1]
        res = RESULTS.get(token)
        if not res:
            send_text(chat_id, "⛔ Нет данных для выдачи. Сформируйте QR заново.", thread_id)
            return
        fields: InvoiceFields = res["fields"]
        st = res["st"]
        qr_png = res["qr_png"]
        # Отправим текстовый файл с ST00012 и сам QR ещё раз как документ
        st_bytes = st.encode("utf-8")
        tg_api_upload_document("payment_st00012.txt", st_bytes, {
            "chat_id": chat_id,
            "message_thread_id": thread_id or ""
        })
        send_doc(chat_id, "qr.png", qr_png, caption="Повтор QR.", thread_id=thread_id)
        return

    if data.startswith("cancel:"):
        token = data.split(":", 1)[1]
        RESULTS.pop(token, None)
        send_text(chat_id, "✖ Готово. Сессию результата очистил.", thread_id)
        return

    send_text(chat_id, "🤔 Неизвестная команда.", thread_id)

def handle_update(update: Dict[str, Any]) -> None:
    # callback_query
    cq = update.get("callback_query")
    if cq:
        handle_callback(cq)
        return

    # message
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

        token = uuid.uuid4().hex[:24]
        PENDING[token] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "file_id": file_id,
            "file_name": file_name,
            "message_id": message_id,
            "token": token,
        }
        send_text(
            chat_id,
            f"Получен файл: *{file_name}*\n\nПодтвердить обработку?",
            thread_id,
            reply_to=message_id,
            reply_markup=kb_confirm(token)
        )
        return

    if "text" in message:
        send_text(chat_id, "👋 Отправьте PDF/DOCX *файлом* (скрепкой). После загрузки появятся кнопки подтверждения.", thread_id)
        return

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
