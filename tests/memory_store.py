"""An in-memory Store for engine and backend tests (also handy as a reference implementation)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from jornada.sync.base import Item, StoreError


class MemoryStore:
    name = "memory"

    def __init__(self, records: Dict[str, Any] = None, fail_on: Tuple[str, ...] = ()) -> None:
        self.records: Dict[str, Any] = dict(records or {})
        self.calls: List[Tuple[str, Optional[str]]] = []
        self.fail_on = fail_on
        self._next = 1

    def _check(self, op: str) -> None:
        if op in self.fail_on:
            raise StoreError(f"{op} refused by test store")

    def list(self) -> Tuple[Item, ...]:
        self.calls.append(("list", None))
        return tuple(Item(id=k, record=v) for k, v in self.records.items())

    def create(self, record: Any) -> str:
        self._check("create")
        item_id = f"m{self._next}"
        self._next += 1
        self.records[item_id] = record
        self.calls.append(("create", item_id))
        return item_id

    def update(self, item_id: str, record: Any) -> Optional[str]:
        self._check("update")
        if item_id not in self.records:
            raise StoreError(f"no record {item_id}")
        self.records[item_id] = record
        self.calls.append(("update", item_id))
        return None

    def delete(self, item_id: str) -> None:
        self._check("delete")
        if item_id not in self.records:
            raise StoreError(f"no record {item_id}")
        del self.records[item_id]
        self.calls.append(("delete", item_id))
