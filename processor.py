# processor.py — GPT-разбор инвойса и генерация GOST (ST00012) QR
# v2.6: PDF-сканы → рендер в изображения через PyMuPDF (fitz) и отправка в GPT
from __future__ import annotations
import io
import os
import re
import json
import csv
import base64
import logging
from typing import Tuple, Optional, List

from openai import OpenAI
import qrcode  # pillow обязателен

from telegram.ext import ContextTypes
from store import store

log = logging.getLogger("processor")

NBSP = "\u00A0"
ST00012_REQUIRED = ["Name", "PersonalAcc", "BankName", "BIC", "CorrespAcc", "Sum", "Purpose"]

# ---------------- utils ----------------
def _qr_png_bytes(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def _to_data_uri(b: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(b).decode('ascii')}"

def _guess_mime_for_photo() -> str:
    return "image/jpeg"

# ---------------- local extractors ----------------
INV_NUM_RE  = re.compile(r"(?:Сч[её]т(?:\s*на\s*оплату)?\s*№\s*([0-9\-]+))", re.IGNORECASE)
INV_DATE_RE = re.compile(r"от\s*([0-9]{1,2}[.\s][0-9]{1,2}[.\s][0-9]{2,4}|[0-9]{1,2}\s+[А-Яа-яЁёA-Za-z]+?\s+\d{4})")
VAT_PCT_RE  = re.compile(r"(?:НДС|VAT)\s*([0-9]{1,2})\s*%")
VAT_SUM_RE  = re.compile(r"(?:НДС[:\s]|VAT[:\s]).{0,20}?([0-9\s\u00A0]+(?:[.,][0-9]{1,2})?)", re.IGNORECASE)
TOTAL_RE    = re.compile(r"(?:Всего\s*к\s*оплате|Итого|Total).{0,20}?([0-9\s\u00A0]+(?:[.,][0-9]{1,2})?)", re.IGNORECASE)

def _pdf_to_text(file_bytes: bytes) -> str:
    """Тянем «живой» текст из PDF. Для сканов вернётся пусто или упадёт — это ок, поймаем и уйдём в рендер-как-картинки."""
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

def _pdf_to_images(file_bytes: bytes, max_pages: int = 3, dpi: int = 200) -> List[bytes]:
    """Рендерим первые страницы PDF в PNG через PyMuPDF (fitz). Подходит для сканов."""
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        log.warning("PyMuPDF not available: %s", e)
        return []

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        log.warning("fitz.open failed: %s", e)
        return []

    images = []
    try:
        pages = min(len(doc), max_pages)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for i in range(pages):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            images.append(pix.tobytes("png"))
    except Exception as e:
        log.warning("PDF render to images failed: %s", e)
    finally:
        doc.close()
    return images

def _xls_to_text(file_bytes: bytes) -> str:
    """Старый Excel (.xls, OLE2) — через xlrd==1.2.0"""
    try:
        import xlrd  # type: ignore
        book = xlrd.open_workbook(file_contents=file_bytes)
        out = []
        for si in range(book.nsheets):
            sh = book.sheet_by_index(si)
            for ri in range(sh.nrows):
                row = sh.row_values(ri)
                vals = [str(v) if v is not None else "" for v in row]
                if any(vals):
                    out.append(" | ".join(vals))
        return "\n".join(out)
    except Exception as e:
        log.warning("XLS extract failed: %s", e)
        return ""

def _xlsx_to_text(file_bytes: bytes) -> str:
    """Новый Excel (.xlsx/.xlsm) — через openpyxl"""
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

def _csv_like_to_text(file_bytes: bytes) -> str:
    """Попытка распознать CSV/TSV/plain-text таблицы."""
    encodings = ("utf-8", "cp1251", "latin-1")
    text = ""
    for enc in encodings:
        try:
            text = file_bytes.decode(enc, errors="strict")
            break
        except Exception:
            continue
    if not text:
        text = file_bytes.decode("utf-8", errors="ignore")

    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(text[:4096])
        reader = csv.reader(text.splitlines(), dialect)
        rows = [" | ".join(row) for row in reader if any(cell.strip() for cell in row)]
        return "\n".join(rows)
    except Exception:
        return text

def _excel_to_text(file_bytes: bytes) -> str:
    """Авто-детект Excel: xlsx/xls/csv."""
    b0 = file_bytes[:8]
    # zip сигнатура (xlsx/xlsm/xltx…)
    if b0.startswith(b"PK\x03\x04"):
        txt = _xlsx_to_text(file_bytes)
        if txt:
            return txt
    # OLE2 сигнатура (xls)
    if b0.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"):
        txt = _xls_to_text(file_bytes)
        if txt:
            return txt
    # иначе попробуем как CSV/TSV/plain
    return _csv_like_to_text(file_bytes)

def _normalize_money(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.replace(NBSP, " ").replace(" ", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        try:
            return float(s.replace(".", ""))
        except Exception:
            return None

def _pre_hint(text: str) -> dict:
    t = (text or "").replace(NBSP, " ")
    inv_num = inv_date = None
    vat_pct = vat_sum = total = None

    m = INV_NUM_RE.search(t)
    if m: inv_num = m.group(1).strip()

    m = INV_DATE_RE.search(t)
    if m: inv_date = m.group(1).strip()

    m = VAT_PCT_RE.search(t)
    if m:
        try: vat_pct = int(m.group(1))
        except Exception: pass

    m = VAT_SUM_RE.search(t)
    if m:
        v = _normalize_money(m.group(1))
        if v is not None: vat_sum = v

    m = TOTAL_RE.search(t)
    if m:
        v = _normalize_money(m.group(1))
        if v is not None: total = v

    return {
        "invoice_number": inv_num,
        "invoice_date": inv_date,
        "vat_percent": vat_pct,   # %
        "vat_amount": vat_sum,    # рубли
        "total": total,           # рубли
    }

# ---------------- validate & caption ----------------
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

# ---------------- GPT (Chat Completions + strict JSON) ----------------
GPT_MODEL = os.getenv("GPT_INVOICE_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = (
    "Ты финансовый парсер инвойсов. По входному контенту (текст PDF/таблицы или изображение) найди реквизиты "
    "для платежного QR по стандарту GOST ST00012 (Россия). Отвечай строго JSON-объектом: "
    '{"st":"ST00012|Name=...|PersonalAcc=...|BankName=...|BIC=...|CorrespAcc=...|Sum=...|Purpose=...",'
    '"fields":{"Name":"...","PersonalAcc":"...","BankName":"...","BIC":"...","CorrespAcc":"...","Sum":"...","Purpose":"..."},'
    '"notes":"..."} '
    "Требования: Sum — целое число копеек (например, 179500 для 1 795,00). "
    "Purpose обязательно: если есть НДС — укажи «НДС X% — Y ₽», если нет — «без НДС». "
    "Если видишь номер и дату счёта, добавь «Оплата по счёту №… от …». "
    "Не выдумывай данные: если чего-то нет — укажи это в notes."
)

def _client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return OpenAI(api_key=api_key)

def _parse_json(text: str) -> dict:
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        raise ValueError("no JSON object found")
    return json.loads(text[s:e+1])

async def _call_gpt_on_file(file_bytes: bytes, file_type: str, prehint: dict) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    """Возвращает (st_string, fields_dict, notes) либо (None, None, reason)."""
    try:
        client = _client()
    except Exception as e:
        return None, None, f"OpenAI key/client error: {e}"

    # Собираем контент под тип
    if file_type == "photo":
        mime = _guess_mime_for_photo()
        data_uri = _to_data_uri(file_bytes, mime)
        user_content = [
            {"type": "text", "text": "Извлеки реквизиты по изображению счёта и верни JSON как описано."},
            {"type": "text", "text": f"Подсказки (если релевантны): {json.dumps(prehint, ensure_ascii=False)}"},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
    elif file_type == "document":
        txt = _pdf_to_text(file_bytes)
        if txt and txt.strip():
            user_content = [
                {"type": "text", "text": "Ниже текст из PDF-счёта. Верни JSON как описано."},
                {"type": "text", "text": f"Подсказки (если релевантны): {json.dumps(prehint, ensure_ascii=False)}"},
                {"type": "text", "text": txt[:15000]},
            ]
        else:
            # Фолбэк для сканов: рендерим страницы в картинки и отправляем их как image_url
            images = _pdf_to_images(file_bytes, max_pages=3, dpi=220)
            if not images:
                return None, None, "PDF is a scan and could not be rendered to images"
            user_content = [
                {"type": "text", "text": "PDF выглядит как скан. Проанализируй изображения страниц и верни JSON как описано."},
                {"type": "text", "text": f"Подсказки (если релевантны): {json.dumps(prehint, ensure_ascii=False)}"},
            ]
            for img in images:
                user_content.append({"type": "image_url", "image_url": {"url": _to_data_uri(img, "image/png")}})
    elif file_type == "excel":
        txt = _excel_to_text(file_bytes)
        user_content = [
            {"type": "text", "text": "Ниже текстовое представление Excel/CSV-счёта. Верни JSON как описано."},
            {"type": "text", "text": f"Подсказки (если релевантны): {json.dumps(prehint, ensure_ascii=False)}"},
            {"type": "text", "text": txt[:15000] if txt else ""},
        ]
    else:
        # safety fallback
        txt = _pdf_to_text(file_bytes)
        user_content = [
            {"type": "text", "text": "Ниже текст из документа. Верни JSON как описано."},
            {"type": "text", "text": f"Подсказки (если релевантны): {json.dumps(prehint, ensure_ascii=False)}"},
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

# ---------------- main entry ----------------
async def on_approved_send_qr(context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, status_msg_id: int) -> None:
    inv = store.get(status_msg_id)
    if not inv or not inv.get("src"):
        log.warning("No source bound to status_msg_id=%s", status_msg_id)
        return

    src = inv["src"]
    file_id = src["file_id"]
    file_type = src["file_type"]   # "document" | "photo" | "excel"
    thread_id = src.get("thread_id")

    tg_file = await context.bot.get_file(file_id)
    fb = await tg_file.download_as_bytearray()
    b = bytes(fb)

    # подсказки для GPT (если есть текст)
    base_text = ""
    if file_type == "document":
        base_text = _pdf_to_text(b)
    elif file_type == "excel":
        base_text = _excel_to_text(b)
    prehint = _pre_hint(base_text)

    # GPT → ST00012
    st, fields, notes = await _call_gpt_on_file(b, file_type, prehint)

    caption = ""
    if st:
        err = _validate_st00012(st)
        if err:
            caption = f"⚠️ Некорректный GOST QR: {err}"
            st = None
    else:
        caption = f"⚠️ GPT не вернул ST00012. {notes or ''}".strip()

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

    # fallback
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
