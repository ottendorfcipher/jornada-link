"""Small helpers shared by the notes backends: time stamps and title lines."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ...pim.timeconv import parse_iso, to_wall_clock

_UNIX_EPOCH = datetime(1970, 1, 1)
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def wall_clock_from_iso(text: Any) -> Optional[datetime]:
    """An ISO 8601 stamp (``Z`` or an offset accepted) → naive local wall-clock; None when unusable."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        moment = parse_iso(text)
    except ValueError:
        return None
    if not isinstance(moment, datetime):
        moment = datetime(moment.year, moment.month, moment.day)
    return to_wall_clock(moment).replace(microsecond=0)


def wall_clock_from_mtime(seconds: Optional[float]) -> Optional[datetime]:
    """A Mac file's modification time (Unix seconds) → naive local wall-clock."""
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def device_wall_clock(seconds: Optional[float]) -> Optional[datetime]:
    """A device file time as :mod:`jornada.rapi` reports it → the wall-clock the Jornada displays.

    Device FILETIMEs are naive wall-clock values (see :mod:`jornada.pim.timeconv`), so the
    calendar fields are read straight off the epoch offset without any zone shift.
    """
    if not seconds:
        return None
    try:
        return (_UNIX_EPOCH + timedelta(seconds=seconds)).replace(microsecond=0)
    except (OverflowError, ValueError):
        return None


def core_data_to_wall_clock(seconds: Any) -> Optional[datetime]:
    """Core Data's seconds since 2001-01-01 UTC (Bear's ``ZMODIFICATIONDATE``) → naive local wall-clock."""
    if seconds is None:
        return None
    try:
        moment = _CORE_DATA_EPOCH + timedelta(seconds=float(seconds))
    except (TypeError, ValueError, OverflowError):
        return None
    return to_wall_clock(moment).replace(microsecond=0)


def drop_title_line(text: str, title: str) -> str:
    """Remove a leading line that only repeats the title (``Title`` or ``# Title``).

    Apple Notes and Bear both keep the title as the first line of the body; the
    neutral record carries it separately, so that line is not part of the body.
    """
    first, separator, rest = text.partition("\n")
    if title.strip() and first.strip().lstrip("#").strip() == title.strip():
        return rest if separator else ""
    return text


def heading_text(title: str, body: str) -> str:
    """The inverse of :func:`drop_title_line` for Markdown-minded apps: ``# Title`` then the body."""
    return f"# {title.strip()}\n{body}"
