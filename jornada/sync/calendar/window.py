"""The sync window: only appointments overlapping ``[start, end)`` take part in a sync.

Both sides are wrapped in a :class:`WindowedStore`, so a record outside the
window is simply absent on both and the engine unlinks it without deleting
anything. The same wrapper hides recurring records when the module's
``recurring`` setting is ``skip`` (the default): a device series cannot be
written to a service as a series and a service series cannot be written to the
device at all, so neither side gets to see the other's. A record a side could not
decode (``Item.unreadable``) is passed through untouched: the engine leaves it and
its counterpart alone, whereas hiding it would read as a deletion.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from ...pim.timeconv import midnight
from ..accounts import Account, AccountError
from ..base import Item, Store
from ..registry import BuildContext

PAST_SETTING = "window_past"
FUTURE_SETTING = "window_future"
RECURRING_SETTING = "recurring"
DEFAULT_PAST_DAYS = 30
DEFAULT_FUTURE_DAYS = 365
MAX_DAYS = 36_500
RECURRING_SKIP = "skip"
RECURRING_FIRST = "first"
RECURRING_MODES = (RECURRING_SKIP, RECURRING_FIRST)
NOW_KEY = "now"   # BuildContext.extra hook: a naive local datetime standing in for "now"


def _days(account: Account, key: str, default: int) -> int:
    raw = (account.setting(key) or "").strip()
    if not raw:
        return default
    try:
        days = int(raw)
    except ValueError:
        raise AccountError(f"account {account.name!r}: {key} must be a whole number of days, not {raw!r}") from None
    if not 0 <= days <= MAX_DAYS:
        raise AccountError(f"account {account.name!r}: {key} must be between 0 and {MAX_DAYS} days")
    return days


def sync_window(account: Account, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """``(start, end)`` as naive local datetimes: midnight ``window_past`` days ago up to, but
    excluding, the midnight after the day ``window_future`` days ahead."""
    today = midnight(now if now is not None else datetime.now())
    past = _days(account, PAST_SETTING, DEFAULT_PAST_DAYS)
    future = _days(account, FUTURE_SETTING, DEFAULT_FUTURE_DAYS)
    return today - timedelta(days=past), today + timedelta(days=future + 1)


def recurring_mode(account: Account) -> str:
    mode = (account.setting(RECURRING_SETTING) or "").strip().lower() or RECURRING_SKIP
    if mode not in RECURRING_MODES:
        raise AccountError(f"account {account.name!r}: {RECURRING_SETTING} must be "
                           f"{' or '.join(RECURRING_MODES)}, not {mode!r}")
    return mode


def skips_recurring(account: Account) -> bool:
    return recurring_mode(account) == RECURRING_SKIP


def in_window(record: Any, start: datetime, end: datetime) -> bool:
    """True when the record's span overlaps ``[start, end)``."""
    first, last = getattr(record, "start", None), getattr(record, "end", None)
    if not isinstance(first, datetime) or not isinstance(last, datetime):
        return False
    return last > start and first < end


class WindowedStore:
    """Store protocol over another store, listing only the records inside the window."""

    def __init__(self, inner: Store, start: datetime, end: datetime, skip_recurring: bool = False) -> None:
        if end <= start:
            raise ValueError("the sync window must end after it starts")
        self._inner = inner
        self._start = start
        self._end = end
        self._skip_recurring = skip_recurring

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def inner(self) -> Store:
        return self._inner

    @property
    def window(self) -> Tuple[datetime, datetime]:
        return self._start, self._end

    @property
    def skip_recurring(self) -> bool:
        return self._skip_recurring

    def list(self) -> Tuple[Item, ...]:
        return tuple(item for item in self._inner.list() if self._keeps(item))

    def _keeps(self, item: Item) -> bool:
        if item.unreadable:
            return True
        if self._skip_recurring and bool(getattr(item.record, "recurring", False)):
            return False
        return in_window(item.record, self._start, self._end)

    def create(self, record: Any) -> str:
        return self._inner.create(record)

    def update(self, item_id: str, record: Any) -> Optional[str]:
        return self._inner.update(item_id, record)

    def delete(self, item_id: str) -> None:
        self._inner.delete(item_id)


def resolve_window(account: Account, context: BuildContext) -> Tuple[datetime, datetime]:
    """The account's window; ``context.extra["now"]`` (a naive datetime) stands in for now."""
    now = context.extra.get(NOW_KEY)
    return sync_window(account, now if isinstance(now, datetime) else None)


def windowed(inner: Store, account: Account, start: datetime, end: datetime) -> WindowedStore:
    """Wrap a store with the account's window and its recurring policy."""
    return WindowedStore(inner, start, end, skip_recurring=skips_recurring(account))


__all__ = ["WindowedStore", "sync_window", "recurring_mode", "skips_recurring", "in_window", "resolve_window",
           "windowed", "PAST_SETTING", "FUTURE_SETTING", "RECURRING_SETTING", "DEFAULT_PAST_DAYS",
           "DEFAULT_FUTURE_DAYS", "RECURRING_SKIP", "RECURRING_FIRST", "RECURRING_MODES", "NOW_KEY"]
