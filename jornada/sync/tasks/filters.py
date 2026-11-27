"""The module's ``completed`` setting: sync completed tasks like any other, or hide them.

``keep`` (the default) leaves both stores as they are. ``hide`` wraps both
sides in :class:`CompletedFilter`: completed tasks are left out of the
listings, so they are never created on the other side. A task that vanishes
from one listing (completed there, or deleted) is then *marked completed* on
the other side instead of deleted — nothing is ever deleted in this mode — and
once both copies are complete the engine unlinks the pair.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Callable, Dict, Optional, Tuple

from ..accounts import Account, AccountError
from ..base import Item, Store
from ..registry import SettingSpec
from .common import Log

COMPLETED_KEY = "completed"
KEEP = "keep"
HIDE = "hide"
COMPLETED_MODES = (KEEP, HIDE)
COMPLETED_SETTING = SettingSpec(
    COMPLETED_KEY,
    "completed tasks: keep (default: they sync like open ones) or hide (they are left out of both sides, "
    "so they are never created on the other side; a task that disappears from one side is marked completed "
    "on the other instead of deleted, and the pair is then unlinked)",
    required=False, default=KEEP,
)


def completed_mode(account: Account) -> str:
    """The account's ``completed`` mode, validated."""
    value = (account.setting(COMPLETED_KEY, KEEP) or KEEP).strip().lower()
    if value not in COMPLETED_MODES:
        raise AccountError(f"setting {COMPLETED_KEY} must be one of {', '.join(COMPLETED_MODES)}, not {value!r}")
    return value


def wrap_completed(store: Store, account: Account, log: Log = lambda _line: None,
                   today: Callable[[], date] = date.today) -> Store:
    """``store`` itself in keep mode, or wrapped in a :class:`CompletedFilter` in hide mode."""
    if completed_mode(account) == HIDE:
        return CompletedFilter(store, log=log, today=today)
    return store


class CompletedFilter:
    """A Store that hides completed tasks and completes instead of deleting (see the module docs)."""

    def __init__(self, inner: Store, log: Log = lambda _line: None, today: Callable[[], date] = date.today) -> None:
        self._inner = inner
        self._log = log
        self._today = today
        self._open: Dict[str, Item] = {}

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def inner(self) -> Store:
        return self._inner

    def list(self) -> Tuple[Item, ...]:
        items = self._inner.list()
        visible = tuple(item for item in items if not item.record.is_completed)
        self._open = {item.id: item for item in visible}
        return visible

    def create(self, record: Any) -> str:
        item_id = self._inner.create(record)
        self._remember(item_id, record)
        return item_id

    def update(self, item_id: str, record: Any) -> Optional[str]:
        version = self._inner.update(item_id, record)
        self._remember(item_id, record)
        return version

    def delete(self, item_id: str) -> None:
        item = self._open.get(item_id) or self._refresh(item_id)
        if item is None:
            self._log(f"{self.name}: {item_id} is already gone or completed; nothing to delete")
            return
        self._inner.update(item_id, replace(item.record, completed=self._today()))
        self._log(f"{self.name}: marked {_label(item.record)!r} completed instead of deleting it (completed=hide)")

    def _refresh(self, item_id: str) -> Optional[Item]:
        self.list()
        return self._open.get(item_id)

    def _remember(self, item_id: str, record: Any) -> None:
        """Keep the cache current so a later delete completes the record as it was last written."""
        if record.is_completed:
            self._open.pop(item_id, None)
        else:
            self._open[item_id] = Item(id=item_id, record=record)


def _label(record: Any) -> str:
    summary = getattr(record, "summary", "")
    return summary.strip()[:60] if isinstance(summary, str) else "(task)"


__all__ = ["COMPLETED_KEY", "COMPLETED_MODES", "COMPLETED_SETTING", "KEEP", "HIDE", "CompletedFilter",
           "completed_mode", "wrap_completed"]
