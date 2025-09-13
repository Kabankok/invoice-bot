# processor.py — извлечение суммы/назначения (PDF/Excel) и генерация QR
from __future__ import annotations
import io
import re
import logging
from typing import Tuple, Optional

import qrcode  # требует pillow
from telegram.ext import ContextTypes

from store import store, APPROVED

log = logging.getLogger("processor")

# ----------------- helpers -----------------
MONEY_RE = re.compile(
    r"(?:итог|к оплате|сумма|итого|total)[^0-9\-.,]{0,20}([0-9]{1,3}(?:[ .]?[0-9]{3})*(?:[.,][0-9]{1,2})?)",
    re.IGNORECASE | re.UNICODE,
)
DESC_RE = re.compile(r"(?:назначение платежа|за что|основание|purpose)[:\-–]\s*(.+)", re.IGNORECASE)

def _normalize_amount(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().replace(" ", "").replace("\u00a0", "")
    s = s.replace(",", ".")
    try:
        val = float(s)
        return f"{val:.2f}"
    except Exception:
        # вариант с тысячами через точки: 12.345,67 -> 12345.67
        s2 = s.replace(".", "")
        try:
            val = float(s2)
            return f"{val:.2f}"
        except Exception:
            return None

def _qr_png_bytes(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ----------------- PDF -----------------
def _pdf_to_text(file_bytes: bytes) -> str:
    try:
        from PyPDF2 import PdfReader  # lazy import
        r = PdfReader(io.BytesIO(file_bytes))
        chunks = []
        for page in r.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                pass
        return "\n".join(chunks)
    except Exception as e:
        log.warning("PDF parse failed: %s", e)
        return ""

# ----------------- Excel -----------------
def _excel_to_text(file_bytes: bytes) -> str:
    try:
        from openpyxl import load_workbook  # lazy import
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        out = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                vals = [str(v) if v is not None else "" for v in row]
                if any(vals):
                    out.append(" | ".join(vals))
        return "\n".join(out)
    except Exception as e:
        log.warning("XLSX parse failed: %s", e)
        return ""

# ----------------- extraction -----------------
def _extract_from_text(txt: str) -> Tuple[Optional[str], Optional[str]]:
    amount = None
    desc = None

    m = MONEY_RE.search(txt)
    if m:
        amount = _normalize_amount(m.group(1))

    m2 = DESC_RE.search(txt)
    if m2:
        # урезаем слишком длинные хвосты
        desc = m2.group(1).strip()
        if len(desc) > 160:
            desc = desc[:157] + "..."

    return amount, desc

async def _extract_payment_payload(file_bytes: bytes, file_type: str) -> Tuple[str, str]:
    """
    Возвращает (payload_for_qr, human_caption).
    Сейчас: пытаемся достать сумму и назначение из PDF/Excel.
    Фото пока без OCR — оставляем демо-значения.
    """
    text = ""
    if file_type == "document":
        # пробуем как PDF
        text = _pdf_to_text(file_bytes)
        if not text:
            # возможно, это не PDF → оставим как есть
            pass
    elif file_type == "photo":
        # OCR не включаем на этом шаге
        pass
    else:
        # попробуем как Excel
        text = _excel_to_text(file_bytes)

    amount, desc = _extract_from_text(text) if text else (None, None)

    # дефолты, если не нашли
    amount = amount or "0.00"
    desc = desc or "Invoice"

    # простой универсальный payload (позже заменим на формат банка/SBP)
    payload = f"PAYMENT|AMOUNT={amount}|CURRENCY=RUB|DESC={desc}"

    caption = f"QR для оплаты\nСумма: {amount} RUB\nНазначение: {desc}"
    return payload, caption

# ----------------- main entry -----------------
async def on_approved_send_qr(context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, status_msg_id: int) -> None:
    inv = store.get(status_msg_id)
    if not inv or not inv.get("src"):
        log.warning("No source bound to status_msg_id=%s", status_msg_id)
        return

    src = inv["src"]
    file_id = src["file_id"]
    file_type = src["file_type"]
    thread_id = src.get("thread_id")

    tg_file = await context.bot.get_file(file_id)
    fb = await tg_file.download_as_bytearray()

    payload, caption = await _extract_payment_payload(bytes(fb), file_type)
    png = _qr_png_bytes(payload)

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=png,
        caption=caption,
        message_thread_id=thread_id if thread_id else None,
        reply_to_message_id=status_msg_id,
    )

    log.info("QR sent for status_msg_id=%s", status_msg_id)
