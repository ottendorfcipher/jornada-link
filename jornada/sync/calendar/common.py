"""Helpers shared by the calendar backends.

Local-zone conversions that are right on every day of the year, the all-day
span arithmetic every backend needs, and JSON calls whose failures become
user-facing :class:`~jornada.sync.base.StoreError`\\s.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...pim.timeconv import midnight, to_aware, to_wall_clock
from ...webapi.http import HttpClient, HttpError, HttpResponse
from ..base import StoreError

Log = Callable[[str], None]
ONE_DAY = timedelta(days=1)
LOCALTIME_LINK = "/etc/localtime"
_ZONEINFO_MARKER = "zoneinfo/"
_SPACES = re.compile(r"\s+")


# -- local time ---------------------------------------------------------------------
def local_aware(moment: datetime) -> datetime:
    """Naive local wall-clock → aware, with the UTC offset in force *at that moment*.

    ``timeconv.local_zone()`` is the offset of right now; a calendar reaches across
    DST changes, so this goes through :meth:`datetime.astimezone`, which asks the
    C library for the offset of the moment itself. Aware values pass through.
    """
    if moment.tzinfo is not None:
        return moment
    try:
        return moment.astimezone()
    except (OverflowError, OSError, ValueError):
        return to_aware(moment)


def local_wall_clock(moment: datetime) -> datetime:
    """Aware → naive local wall-clock (DST-correct); naive (floating) values pass through.

    A moment the local zone cannot represent (the very edges of the datetime range)
    keeps its own fields, as if it were floating, instead of failing a whole listing.
    """
    if moment.tzinfo is None:
        return moment
    try:
        return moment.astimezone().replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return _fixed_offset_wall_clock(moment)


def _fixed_offset_wall_clock(moment: datetime) -> datetime:
    try:
        return to_wall_clock(moment)
    except (OverflowError, OSError, ValueError):
        return moment.replace(tzinfo=None)


def iso_offset(moment: datetime) -> str:
    """RFC 3339 text with the local UTC offset, e.g. ``2026-09-07T09:00:00+02:00``."""
    return local_aware(moment).replace(microsecond=0).isoformat(timespec="seconds")


def local_zone_name(environ: Mapping[str, str] = os.environ, localtime: str = LOCALTIME_LINK) -> Optional[str]:
    """The IANA name of the Mac's zone (``TZ``, else the ``/etc/localtime`` link), or None."""
    candidates: List[str] = [environ.get("TZ", "").strip().lstrip(":")]
    try:
        target = os.readlink(localtime)
    except OSError:
        target = ""
    marker = target.find(_ZONEINFO_MARKER)
    if marker >= 0:
        candidates.append(target[marker + len(_ZONEINFO_MARKER):])
    return next((name for name in candidates if name and _known_zone(name)), None)


def _known_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


# -- all-day spans -----------------------------------------------------------------------
def all_day_end(start: datetime, end: datetime) -> datetime:
    """The exclusive midnight ending an all-day span (an end inside a day includes that day)."""
    first = midnight(start)
    last = midnight(end) + (ONE_DAY if end.time() != time(0) else timedelta(0))
    return last if last > first else first + ONE_DAY


def nearest_midnight(moment: datetime) -> datetime:
    """The midnight closest to ``moment`` (an all-day boundary shifted by a zone offset)."""
    day = midnight(moment)
    return day + ONE_DAY if moment.hour >= 12 else day


# -- HTTP ----------------------------------------------------------------------------------
def send(http: HttpClient, service: str, method: str, path: str, what: str, **kwargs: Any) -> HttpResponse:
    """One request; a connection failure is a StoreError, any HTTP status is returned."""
    try:
        return http.request(method, path, accept_errors=True, **kwargs)
    except HttpError as exc:
        raise StoreError(f"{service}: could not {what}: {exc.message}") from exc


def json_call(http: HttpClient, service: str, method: str, path: str, what: str, **kwargs: Any) -> Any:
    """A request whose reply is JSON (or empty); every failure is a StoreError naming ``what``."""
    response = send(http, service, method, path, what, **kwargs)
    if not response.ok:
        raise StoreError(f"{service}: could not {what}: HTTP {response.status}{excerpt(response)}")
    try:
        return response.json()
    except HttpError as exc:
        raise StoreError(f"{service}: could not {what}: {exc.message}") from exc


def excerpt(response: HttpResponse, limit: int = 200) -> str:
    """A short, whitespace-collapsed piece of an error body for messages."""
    words = _SPACES.sub(" ", response.text[:1000]).strip()
    return f" ({words[:limit]})" if words else ""


def json_object(payload: Any, service: str, what: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"{service}: unexpected reply while trying to {what}")
    return payload


def string_field(payload: Any, service: str, what: str, key: str = "id") -> str:
    value = json_object(payload, service, what).get(key)
    if not isinstance(value, str) or not value:
        raise StoreError(f"{service} did not report the {key} of the event it should {what}")
    return value


def text_of(value: Any) -> str:
    return value if isinstance(value, str) else ""


__all__ = ["Log", "ONE_DAY", "local_aware", "local_wall_clock", "iso_offset", "local_zone_name", "all_day_end",
           "nearest_midnight", "send", "json_call", "excerpt", "json_object", "string_field", "text_of"]
