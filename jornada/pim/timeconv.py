"""Time conventions between the device, the neutral records and the Mac.

The Jornada keeps one clock and (as set up by ``jornada settime``) that clock
reads the Mac's local wall-clock, so a FILETIME on the device is treated as a
**naive wall-clock** value: its calendar fields are what the device displays.
Neutral records use the same naive wall-clock convention; the modern side
converts to and from the Mac's local zone (or an explicit zone) at its edge.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional, Union

from ..constants import FILETIME_EPOCH_DELTA

_EPOCH = datetime(1601, 1, 1)
_TICKS_PER_SECOND = 10_000_000


def filetime_to_naive(ticks: int) -> Optional[datetime]:
    """FILETIME ticks → naive datetime whose fields are the device wall-clock (None if 0)."""
    if ticks <= 0:
        return None
    return _EPOCH + timedelta(microseconds=ticks // 10)


def naive_to_filetime(moment: datetime) -> int:
    """Naive wall-clock datetime → FILETIME ticks (aware inputs are converted first)."""
    naive = to_wall_clock(moment)
    delta = naive - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _TICKS_PER_SECOND + delta.microseconds * 10


def date_to_filetime(day: date) -> int:
    return naive_to_filetime(datetime(day.year, day.month, day.day))


def filetime_to_date(ticks: int) -> Optional[date]:
    moment = filetime_to_naive(ticks)
    return None if moment is None else moment.date()


def local_zone() -> tzinfo:
    """The Mac's current local time zone."""
    return datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc


def to_wall_clock(moment: datetime, zone: Optional[tzinfo] = None) -> datetime:
    """Aware datetime → naive wall-clock in ``zone`` (default: local); naive passes through."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(zone or local_zone()).replace(tzinfo=None)


def to_aware(moment: datetime, zone: Optional[tzinfo] = None) -> datetime:
    """Naive wall-clock → aware datetime in ``zone`` (default: local); aware passes through."""
    if moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=zone or local_zone())


def midnight(day: Union[date, datetime]) -> datetime:
    if isinstance(day, datetime):
        return datetime(day.year, day.month, day.day)
    return datetime(day.year, day.month, day.day)


def parse_iso(text: str) -> Union[date, datetime]:
    """ISO 8601 date or datetime (``Z`` accepted) → date / datetime."""
    cleaned = text.strip()
    if len(cleaned) == 10:
        return date.fromisoformat(cleaned)
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    return datetime.fromisoformat(cleaned)
