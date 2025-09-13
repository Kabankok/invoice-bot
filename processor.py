# processor.py — GPT-разбор инвойса и генерация GOST (ST00012) QR
from __future__ import annotations
import io
import os
import re
import json
import base64
import logging
from typing import Tuple, Optional

from openai import OpenAI
import qrcode  # требует pillow

from telegram.ext import ContextTypes
from store import store

log = logging.getLogger("processor")

# ---------- helpers ----------
NBSP = "\u00A0"

ST00012_REQUIRED = [
    "Name", "PersonalAcc", "BankName", "BIC", "CorrespAcc", "Sum", "Purpose"
]

ST00012_RE = re.compile(r"^ST00012\|(.+)$")

def _qr_png_bytes(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def _to_data_uri(image_bytes: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"

def _guess_mime(file_type: str) -> str:
    # очень грубо; для Telegram типов достаточно
    if file_type == "photo":
        return "image/jpeg"
    return "application/pdf"

# ---------- simple extractors for local fallback ----------
def _pdf_to_text(file_bytes: bytes) -> str:
    try:
        from PyPDF2 import PdfReader  # lazy import
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

# ---------- GOST builder & validation ----------
def _validate_st00012(s: str) -> Optional[str]:
    """Проверяем, что это ST00012 и что есть обязательные поля."""
    if not s or not s.startswith("ST00012|"):
        return "payload is not ST00012"
    # Быстрая проверка наличия ключей
    fields = {}
    try:
        parts = s.split("|")[1:]  # без ST00012
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                fields[k] = v
    except Exception:
        return "failed to parse key=value pairs"
    missing = [k for k in ST00012_REQUIRED if k not in fields or not fields[k].strip()]
    if missing:
        return f"missing fields: {', '.join(missing)}"
    # Сумма должна быть в копейках (целое число)
    if not re.fullmatch(r"\d+", fields["Sum"]):
        return "Sum must be integer number of kopecks"
    return None

def _caption_from_fields(fields: dict) -> str:
    name = fields.get("Name", "")
    sum_kopecks = fields.get("Sum", "0")
    try:
        amount = f"{int(sum_kopecks)/100:.2f}"
    except Exception:
        amount = "0.00"
    purpose = fields.get("Purpose", "")
    vat = fields.get("PayeeINN", "")  # если ИНН хотим подсветить; НДС лучше отдавать в Purpose
    # ожидаем, что GPT включит «НДС …» внутрь Purpose, либо добавит поле VAT
    vat_text = ""
    if "VAT=" in purpose.upper() or "НДС" in purpose.upper():
        vat_text = ""  # уже в назначении
    return (
        f"Получатель: {name}\n"
        f"Сумма: {amount} RUB\n"
        f"Назначение: {purpose}"
        f"{'' if not vat_text else f'\\n{vat_text}'}"
    )

# ---------- GPT call ----------
def _gpt_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return OpenAI(api_key=api_key)

GPT_MODEL = os.getenv("GPT_INVOICE_MODEL", "gpt-4o-mini")

INSTRUCTIONS = (
    "Ты финансовый парсер инвойсов. По входному файлу (PDF/изображение/таблица) найди реквизиты "
    "для платежного QR по стандарту GOST ST00012 (Россия). Верни JSON со строкой ST00012 и разобранными полями. "
    "Требования:\n"
    "1) Обязательные поля: Name, PersonalAcc, BankName, BIC, CorrespAcc, Sum (в копейках), Purpose.\n"
    "2) Sum: целое число копеек (напр. 179500 для 1 795,00).\n"
    "3) Purpose: «Оплата по счёту №… от …, ... НДС …% ...» — укажи явно, есть НДС или нет.\n"
    "4) Если данных нет в файле — не выдумывай; верни пояснение в 'notes'.\n"
    "Формат ответа строго:\n"
    "{\n"
    "  \"st\": \"ST00012|Name=...|PersonalAcc=...|BankName=...|BIC=...|CorrespAcc=...|Sum=...|Purpose=...\",\n"
    "  \"fields\": {\"Name\":\"...\",\"PersonalAcc\":\"...\",\"BankName\":\"...\",\"BIC\":\"...\",\"CorrespAcc\":\"...\",\"Sum\":\"...\",\"Purpose\":\"...\"},\n"
    "  \"notes\": \"...\"\n"
    "}\n"
)

async def _call_gpt_on_file(file_bytes: bytes, file_type: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    """
    Возвращает (st_string, fields_dict, notes) либо (None, None, reason)
    """
    client = _gpt_client()

    # Для PDF/фото пойдём как мультимодал: data:URI
    if file_type in ("document", "photo"):
        mime = _guess_mime(file_type)
        data_uri = _to_data_uri(file_bytes, mime)
        content = [
            {"type": "text", "text": INSTRUCTIONS},
            {"type": "input_text", "text": "Проанализируй документ и верни JSON как описано."},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
    else:
        # Excel → превратим в текст (быстрый путь) и отдадим GPT на доинтерпретацию
        txt = _excel_to_text(file_bytes)
        content = [
            {"type": "text", "text": INSTRUCTIONS},
            {"type": "input_text", "text": "Текстовое представление таблицы ниже:\n" + (txt[:15000] if txt else "")},
        ]

    try:
        resp = client.responses.create(
            model=GPT_MODEL,
            input=[{"role": "user", "content": content}],
            temperature=0.0,
        )
        raw = resp.output_text
    except Exception as e:
        return None, None, f"GPT error: {e}"

    # Найдём JSON в ответе
    try:
        # иногда модель может вернуть префикс/суффикс — попытаемся вычленить { ... }
        start = raw.find("{")
        end = raw.rfind("}")
        data = json.loads(raw[start:end+1])
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

    # 1) качаем файл из Telegram
    tg_file = await context.bot.get_file(file_id)
    fb = await tg_file.download_as_bytearray()
    b = bytes(fb)

    # 2) зовём GPT → ждём ST00012
    st, fields, notes = await _call_gpt_on_file(b, file_type)

    # 3) валидация; при провале — пытаемся собрать подпись из локального текста
    caption = ""
    if st:
        err = _validate_st00012(st)
        if err:
            caption = f"⚠️ Некорректный GOST QR: {err}"
            st = None
    else:
        caption = f"⚠️ GPT не вернул строку ST00012. {notes or ''}".strip()

    # 4) если GPT справился — шлём рабочий QR + нормальную подпись
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
            log.info("ST00012 QR sent for %s", status_msg_id)
            return
        except Exception as e:
            caption = f"⚠️ Не удалось сгенерировать QR: {e}"

    # 5) fallback: текст из PDF/Excel и демо-QR (не для оплаты) — чтобы не молчать
    txt = _pdf_to_text(b) if file_type == "document" else (_excel_to_text(b) if file_type != "photo" else "")
    demo_payload = "ST00012|Name=ERROR|PersonalAcc=00000000000000000000|BankName=ERROR|BIC=000000000|CorrespAcc=00000000000000000000|Sum=0|Purpose=Parse failed"
    png = _qr_png_bytes(demo_payload)
    fallback_caption = "Не удалось собрать рабочий QR. Проверьте формат счёта или пришлите образец для обучения."
    if caption:
        fallback_caption = caption + "\n\n" + fallback_caption

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=png,
        caption=fallback_caption,
        message_thread_id=thread_id if thread_id else None,
        reply_to_message_id=status_msg_id,
    )
