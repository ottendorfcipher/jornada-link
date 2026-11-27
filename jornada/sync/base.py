"""The two-sided store contract the engine syncs between."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Tuple


class StoreError(RuntimeError):
    """A store could not perform an operation (message is safe to show the user)."""


@dataclass(frozen=True)
class Item:
    """One record as a store presents it: opaque id, neutral record, optional version tag.

    A store that finds a record it cannot decode still lists it, with ``record=None``
    and ``problem`` set: the engine then leaves that record and its counterpart alone
    instead of reading the absence as a deletion. ``read_only`` marks records the store
    will refuse to change (recurring device appointments).
    """

    id: str
    record: Any
    version: Optional[str] = None
    problem: Optional[str] = None
    read_only: bool = False

    @property
    def unreadable(self) -> bool:
        return self.record is None

    @property
    def fingerprint(self) -> str:
        return "" if self.record is None else self.record.fingerprint()

    @property
    def match_key(self) -> str:
        return "" if self.record is None else self.record.match_key()


class Store(Protocol):
    """Implemented by the device stores and every modern backend."""

    name: str

    def list(self) -> Tuple[Item, ...]: ...

    def create(self, record: Any) -> str: ...

    def update(self, item_id: str, record: Any) -> Optional[str]: ...

    def delete(self, item_id: str) -> None: ...
