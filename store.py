# store.py — хранилище состояний счётов (в памяти, MVP)
from __future__ import annotations

WAIT     = "WAIT"      # Ожидает согласования
APPROVED = "APPROVED"  # Согласован (ждём оплату)
REJECTED = "REJECTED"  # Отклонён (можно указать причину)
PAID     = "PAID"      # Оплачен (ждём получение)
RECEIVED = "RECEIVED"  # Получен/Забран (финал)

class InvoiceStore:
    def __init__(self) -> None:
        # ключ — message_id статусного сообщения бота
        # значение — словарь: {status, reason, kind}
        self.invoices: dict[int, dict] = {}

    def create(self, status_msg_id: int, kind: str = "unknown") -> None:
        self.invoices[status_msg_id] = {"status": WAIT, "reason": "", "kind": kind}

    def set_status(self, status_msg_id: int, status: str) -> None:
        inv = self.invoices.setdefault(status_msg_id, {"status": WAIT, "reason": "", "kind": "unknown"})
        inv["status"] = status

    def set_reason(self, status_msg_id: int, reason: str) -> None:
        inv = self.invoices.setdefault(status_msg_id, {"status": REJECTED, "reason": "", "kind": "unknown"})
        inv["reason"] = reason.strip()

    def set_kind(self, status_msg_id: int, kind: str) -> None:
        inv = self.invoices.setdefault(status_msg_id, {"status": WAIT, "reason": "", "kind": "unknown"})
        inv["kind"] = kind

    def get(self, status_msg_id: int) -> dict | None:
        return self.invoices.get(status_msg_id)

store = InvoiceStore()

# совместимость: создаёт запись и проставляет статус/тип
def store_invoice(status_msg_id: int, status: str = "WAIT", kind: str = "unknown") -> None:
    store.create(status_msg_id, kind=kind)
    store.set_status(status_msg_id, WAIT if status in ("WAIT", "pending") else status)
