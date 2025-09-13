from __future__ import annotations
from typing import Dict, Tuple, Optional


InvoiceKey = Tuple[int, int] # (chat_id, message_id)


STATUS_WAIT = "Ожидает согласования"
STATUS_OK = "Согласован"
STATUS_REJ = "Отклонён"


class InMemoryStore:
def __init__(self) -> None:
self._data: Dict[InvoiceKey, Dict[str, object]] = {}


def put_invoice(self, key: InvoiceKey, kind: str) -> None:
self._data[key] = {"status": STATUS_WAIT, "kind": kind, "status_msg_id": None}


def set_status(self, key: InvoiceKey, status: str) -> None:
if key in self._data:
self._data[key]["status"] = status


def set_status_msg_id(self, key: InvoiceKey, msg_id: int) -> None:
if key in self._data:
self._data[key]["status_msg_id"] = msg_id


def get(self, key: InvoiceKey) -> Optional[Dict[str, object]]:
return self._data.get(key)


STORE = InMemoryStore()