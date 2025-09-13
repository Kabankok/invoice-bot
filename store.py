# store.py
# Хранилище счетов (в памяти, без БД)


class InvoiceStore:
def __init__(self) -> None:
# словарь: message_id -> {status, reason}
self.invoices = {}


def add(self, message_id: int, status: str = "pending", reason: str = "") -> None:
self.invoices[message_id] = {"status": status, "reason": reason}


def update(self, message_id: int, status: str, reason: str = "") -> None:
if message_id in self.invoices:
self.invoices[message_id]["status"] = status
self.invoices[message_id]["reason"] = reason


def get(self, message_id: int):
return self.invoices.get(message_id)


# глобальный объект для доступа из других файлов
store = InvoiceStore()


def store_invoice(message_id: int, status: str = "pending"):
store.add(message_id, status)
