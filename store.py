# store.py — хранилище состояний счётов (в памяти, MVP)
from __future__ import annotations

WAIT     = "WAIT"      # Ожидает согласования
APPROVED = "APPROVED"  # Согласован (ждём оплату/QR)
REJECTED = "REJECTED"  # Отклонён (можно указать причину)
PAID     = "PAID"      # Оплачен (ждём получение)
RECEIVED = "RECEIVED"  # Получен/Забран (финал)

class InvoiceStore:
    def __init__(self) -> None:
        # ключ — message_id статусного сообщения бота (на котором кнопки)
        # значение — словарь: {status, reason, kind, src}
        # src: {chat_id, thread_id, user_msg_id, file_id, file_type}
        self.invoices: dict[int, dict] = {}

    def create(self, status_msg_id: int, kind: str = "unknown") -> None:
        self.invoices[status_msg_id] = {
            "status": WAIT,
            "reason": "",
            "kind": kind,
            "src": None,
        }

    def set_status(self, status_msg_id: int, status: str) -> None:
        inv = self.invoices.setdefault(status_msg_id, {"status": WAIT, "reason": "", "kind": "unknown", "src": None})
        inv["status"] = status

    def set_reason(self, status_msg_id: int, reason: str) -> None:
        inv = self.invoices.setdefault(status_msg_id, {"status": REJECTED, "reason": "", "kind": "unknown", "src": None})
        inv["reason"] = (reason or "").strip()

    def set_kind(self, status_msg_id: int, kind: str) -> None:
        inv = self.invoices.setdefault(status_msg_id, {"status": WAIT, "reason": "", "kind": "unknown", "src": None})
        inv["kind"] = kind

    def set_source(self, status_msg_id: int, *, chat_id: int, thread_id: int | None, user_msg_id: int, file_id: str, file_type: str) -> None:
        inv = self.invoices.setdefault(status_msg_id, {"status": WAIT, "reason": "", "kind": "unknown", "src": None})
        inv["src"] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "user_msg_id": user_msg_id,
            "file_id": file_id,
            "file_type": file_type,
        }

    def get(self, status_msg_id: int) -> dict | None:
        return self.invoices.get(status_msg_id)

store = InvoiceStore()

# совместимость: создаёт запись и проставляет статус/тип
def store_invoice(status_msg_id: int, status: str = "WAIT", kind: str = "unknown") -> None:
    store.create(status_msg_id, kind=kind)
    store.set_status(status_msg_id, WAIT if status in ("WAIT", "pending") else status)
