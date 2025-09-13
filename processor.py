# processor.py — извлечение суммы/назначения (PDF/Excel) и генерация QR
from __future__ import annotations
import io
import re
import logging
from typing import Tuple, Optional

import qrcode  # требует pillow
from telegram.ext import ContextTypes

from store import store

log = logging.getLogger("processor")

# ----------------- Регулярки под русские счета -----------------
# Приоритет: "Всего к оплате" > "Итого"
AMOUNT_PATTERNS = [
    r"Всего\s*к\s*оплате\s*[:\-–]?\s*([0-9\u00A0\s]+(?:[.,][0-9]{1,2})?)",
    r"Итого\s*[:\-–]?\s*([0-9\u00A0\s]+(?:[.,][0-9]{1,2})?)",
    r"Total\s*[:\-–]?\s*([0-9\u00A0\s]+(?:[.,][0-9]{1,2})?)",
]

# "Счёт на оплату № 18277 от 10 сентября 2025"
INV_HEADER_RE = re.compile(
    r"Сч[её]т\s*(?:на\s*оплату)?\s*№\s*([0-9\-]+)\s*от\s*([0-9]{1,2}\s+[A-Za-zА-Яа-яЁё]+?\s+\d{4})",
    re.IGNORECASE
)

# fallback для назначения
DESC_PATTERNS = [
    r"(?:Назначение\s*платежа|Основание|За что|Purpose)[:\-–]\s*(.+)",
]

NBSP = "\u00A0"

def _clean_text(txt: str) -> str:
    # нормализуем пробелы/неразрывные пробелы
    return (
        txt.replace(NBSP, " ")
           .replace("\r", "\n")
    )

def _normalize_amount(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().replace(NBSP, "").replace(" ", "")
    # варианты с разделителями тысяч "1 795,00" -> "1795,00"
    s = s.replace(",", ".")
    try:
        val = float(s)
        return f"{val:.2f}"
    except Exception:
        # если внутри остались точки тысяч (например, "12.345,67" -> "12345.67")
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
                t = page.extract_text() or ""
                chunks.append(t)
            except Exception:
                pass
        return _clean_text("\n".join(chunks))
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
        return _clean_text("\n".join(out))
    except Exception as e:
        log.warning("XLSX parse failed: %s", e)
        return ""

# ----------------- Извлечение из текста -----------------
def _extract_amount(txt: str) -> Optional[str]:
    for pat in AMOUNT_PATTERNS:
        m = re.search(pat, txt, flags=re.IGNORECASE)
        if m:
            amt = _normalize_amount(m.group(1))
            if amt:
                return amt
    return None

def _extract_invoice_header(txt: str) -> Tuple[Optional[str], Optional[str]]:
    m = INV_HEADER_RE.search(txt)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None

def _extract_desc(txt: str) -> Optional[str]:
    # 1) сначала пытаемся вытащить из явного поля "Назначение платежа"/"Основание"/"Purpose"
    for pat in DESC_PATTERNS:
        m = re.search(pat, txt, flags=re.IGNORECASE)
        if m:
            desc = (m.group(1) or "").strip()
            if 3 <= len(desc) <= 180:
                return desc if len(desc) <= 160 else (desc[:157] + "...")
    # 2) если нет — попробуем составить из номера/даты
    inv_num, inv_date = _extract_invoice_header(txt)
    if inv_num and inv_date:
        return f"Оплата по счёту № {inv_num} от {inv_date}"
    # 3) fallback
    return "Invoice"

def _extract_from_text(txt: str) -> Tuple[Optional[str], Optional[str]]:
    amount = _extract_amount(txt)
    desc = _extract_desc(txt)
    return amount, desc

# ----------------- Главный экстрактор -----------------
async def _extract_payment_payload(file_bytes: bytes, file_type: str) -> Tuple[str, str]:
    """
    Возвращает (payload_for_qr, human_caption).
    PDF/Excel: парсим текст, ищем 'Всего к оплате' (в приоритете) и номер/дату для назначения.
    Фото пока без OCR — оставляем демо-подпись.
    """
    text = ""
    if file_type == "document":
        # предположим, что большинство документов — PDF
        text = _pdf_to_text(file_bytes)
        # если пусто, оставим как есть (например, сканы без текста)
    elif file_type == "photo":
        # OCR отключен на этом шаге — оставляем fallback
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

# ----------------- Точка входа после APPROVE -----------------
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

    # 2) парсим и генерим QR
    payload, caption = await _extract_payment_payload(bytes(fb), file_type)
    png = _qr_png_bytes(payload)

    # 3) шлём QR в ту же тему/чат
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=png,
        caption=caption,
        message_thread_id=thread_id if thread_id else None,
        reply_to_message_id=status_msg_id,
    )

    log.info("QR sent for status_msg_id=%s", status_msg_id)

