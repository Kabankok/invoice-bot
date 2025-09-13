# processor.py — GPT-разбор инвойса и генерация GOST (ST00012) QR (v2.2)
from __future__ import annotations
import io
import os
import re
import json
import base64
import logging
from typing import Tuple, Optional

from openai import OpenAI
import qrcode  # pillow обязателен

from telegram.ext import ContextTypes
from store import store

log = logging.getLogger("processor")

NBSP = "\u00A0"
ST00012_REQUIRED = ["Name", "PersonalAcc", "BankName", "BIC", "CorrespAcc", "Sum", "Purpose"]

# ---------- utils ----------
def _qr_png_bytes(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def _to_data_uri(b: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(b).decode('ascii')}"

def _guess_mime(file_type: str) -> str:
    return "image/jpeg" if file_type == "photo" else "application/pdf"

# ---------- local extractors (fallback) ----------
def _pdf_to_text(file_bytes: bytes) -> str:
    try:
        from PyPDF2 import PdfReader
        r = PdfReader(io.BytesIO(file_bytes))
        chunks = []
        for p in r.pages:
            try:
                chunks.append((p.extract_text() or "").replace(NBSP, " "))
            except Exception:
                pass
        return "\n".join(chunks)
    except Exception as e:
        log.warning("PDF extract failed: %s", e)
        return ""

def _excel_to_text(file_bytes: bytes) -> str:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        out = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                vals = [str(v) if v is not None else "" for v in row]
                if any(vals):
                    out.append(" | ".join(vals))
        return "\n".join(out)
    except Exception as e:
        log.warning("XLSX extract failed: %s", e)
        return ""

# ---------- validate & caption ----------
def _validate_st00012(st: str) -> Optional[str]:
    if not st or not st.startswith("ST00012|"):
        return "payload is not ST00012"
    fields = {}
    try:
        for p in st.split("|")[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                fields[k] = v
    except Exception:
        return "failed to parse key=value pairs"

    missing = [k for k in ST00012_REQUIRED if k not in fields or not fields[k].strip()]
    if missing:
        return f"missing fields: {', '.join(missing)}"
    if not re.fullmatch(r"\d+", fields["Sum"]):
        return "Sum must be integer (kopecks)"
    return None

def _caption_from_fields(fields: dict) -> str:
    name = fields.get("Name", "")
    sum_kopecks = fields.get("Sum", "0")
    try:
        amount = f"{int(sum_kopecks)/100:.2f}"
    except Exception:
        amount = "0.00"
    purpose = fields.get("Purpose", "")
    return f"Получатель: {name}\nСумма: {amount} RUB\nНазначение: {purpose}"

# ---------- GPT (Chat Completions + strict JSON) ----------
GPT_MODEL = os.getenv("GPT_INVOICE_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = (
    "Ты финансовый парсер инвойсов. По входному файлу (PDF/изображение/таблица) найди реквизиты "
    "для платежного QR по стандарту GOST ST00012 (Россия). Отвечай строго JSON-объектом: "
    '{"st":"ST00012|Name=...|PersonalAcc=...|BankName=...|BIC=...|CorrespAcc=...|Sum=...|Purpose=...",'
    '"fields":{"Name":"...","PersonalAcc":"...","BankName":"...","BIC":"...","CorrespAcc":"...","Sum":"...","Purpose":"..."},'
    '"notes":"..."} '
    "Требования: Sum — целое число копеек (напр. 179500 для 1 795,00). "
    "Purpose должен явно указывать, есть НДС или нет. Если каких-то данных нет в документе — не выдумывай, укажи это в notes."
)

def _client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return OpenAI(api_key=api_key)

def _parse_json(text: str) -> dict:
    # забираем первый валидный JSON-объект в ответе
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        raise ValueError("no JSON object found")
    return json.loads(text[s:e+1])

async def _call_gpt_on_file(file_bytes: bytes, file_type: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    """
    Возвращает (st_string, fields_dict, notes) либо (None, None, reason)
    """
    try:
        client = _client()
    except Exception as e:
        return None, None, f"OpenAI key/client error: {e}"

    if file_type in ("document", "photo"):
        mime = _guess_mime(file_type)
        data_uri = _to_data_uri(file_bytes, mime)
        user_content = [
            {"type": "text", "text": "Проанализируй документ и верни JSON как описано."},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
    else:
        txt = _excel_to_text(file_bytes)
        user_content = [
            {"type": "text", "text": "Ниже — текстовое представление таблицы. Верни JSON как описано."},
            {"type": "text", "text": txt[:15000] if txt else ""},
        ]

    try:
        resp = client.chat.completions.create(
            model=GPT_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        return None, None, f"GPT error: {e}"

    try:
        data = _parse_json(raw)
    except Exception as e:
        return None, None, f"Bad JSON from GPT: {e}"

    st = (data.get("st") or "").strip()
    fields = data.get("fields") or {}
    notes = data.get("notes") or ""
    return st, fields, notes

# ---------- main entry ----------
async def on_approved_send_qr(context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, status_msg_id: int) -> None:
    inv = store.get(status_msg_id)
    if not inv or not inv.get("src"):
        log.warning("No source bound to status_msg_id=%s", status_msg_id)
        return

    src = inv["src"]
    file_id = src["file_id"]
    file_type = src["file_type"]
    thread_id = src.get("thread_id")

    # 1) качаем файл
    tg_file = await context.bot.get_file(file_id)
    fb = await tg_file.download_as_bytearray()
    b = bytes(fb)

    # 2) GPT → ST00012
    st, fields, notes = await _call_gpt_on_file(b, file_type)

    caption = ""
    if st:
        err = _validate_st00012(st)
        if err:
            caption = f"⚠️ Некорректный GOST QR: {err}"
            st = None
    else:
        caption = f"⚠️ GPT не вернул ST00012. {notes or ''}".strip()

    # 3) успех → отправляем рабочий QR
    if st and fields:
        try:
            png = _qr_png_bytes(st)
            caption = _caption_from_fields(fields)
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=png,
                caption=caption,
                message_thread_id=thread_id if thread_id else None,
                reply_to_message_id=status_msg_id,
            )
            return
        except Exception as e:
            caption = f"⚠️ Не удалось сгенерировать QR: {e}"

    # 4) fallback: демо-QR + пояснение
    demo_payload = "ST00012|Name=ERROR|PersonalAcc=00000000000000000000|BankName=ERROR|BIC=000000000|CorrespAcc=00000000000000000000|Sum=0|Purpose=Parse failed"
    png = _qr_png_bytes(demo_payload)
    fallback_caption = "Не удалось собрать рабочий QR. Проверьте реквизиты или пришлите образец для настройки."
    if caption:
        fallback_caption = caption + "\n\n" + fallback_caption

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=png,
        caption=fallback_caption,
        message_thread_id=thread_id if thread_id else None,
        reply_to_message_id=status_msg_id,
    )
