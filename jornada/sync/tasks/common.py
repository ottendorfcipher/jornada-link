"""Helpers shared by the tasks backends: date stamps, HTML notes and HTTP error mapping.

Every backend turns the dates of its service into plain :class:`datetime.date`
values as the Jornada shows them. A stamp that carries a zone is read in the
Mac's local zone (or the ``zone`` a test supplies) before its date is taken,
and a date written to a service becomes the instant of local midnight, so a
due date survives the round trip on either side of UTC.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Union

from ...pim.tasks import UNKNOWN_COMPLETION_DATE
from ...pim.timeconv import to_aware, to_wall_clock
from ...webapi.http import HttpClient, HttpError, HttpResponse
from ..base import StoreError

Log = Callable[[str], None]
NO_SUBJECT = "(no subject)"

_STAMP = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    r"(?:[T ](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?:\.(?P<fraction>\d+))?)?)?"
    r"\s*(?P<zone>Z|z|[+-]\d{2}:?\d{2})?$"
)
_BLOCK_TAGS = frozenset({"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote"})
_SKIPPED_TAGS = frozenset({"style", "script", "head", "title"})
_MANY_BLANK_LINES = re.compile(r"\n{3,}")


# -- dates -----------------------------------------------------------------------
def parse_stamp(text: Any) -> Union[date, datetime, None]:
    """ISO 8601 date or date-time → date / datetime; None when the text is not one.

    Any number of fraction digits is accepted (Graph writes seven), ``Z`` or an
    offset gives an aware datetime, nothing gives a naive one.
    """
    if not isinstance(text, str):
        return None
    match = _STAMP.match(text.strip())
    if not match:
        return None
    parts = match.groupdict()
    try:
        day = date(int(parts["y"]), int(parts["m"]), int(parts["d"]))
        if parts["hour"] is None:
            return day
        micro = int((parts["fraction"] or "0")[:6].ljust(6, "0"))
        moment = datetime(day.year, day.month, day.day, int(parts["hour"]), int(parts["minute"]),
                          int(parts["second"] or 0), micro)
    except ValueError:
        return None
    return moment.replace(tzinfo=_offset(parts["zone"])) if parts["zone"] else moment


def _offset(text: str) -> tzinfo:
    if text.upper() == "Z":
        return timezone.utc
    digits = text[1:].replace(":", "")
    delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:] or 0))
    return timezone(delta if text[0] == "+" else -delta)


def date_of(text: Any, zone: Optional[tzinfo] = None) -> Optional[date]:
    """The calendar date of an ISO stamp; aware date-times are read in ``zone`` (default: local)."""
    moment = parse_stamp(text)
    if isinstance(moment, datetime):
        return to_wall_clock(moment, zone).date()
    return moment


def completion_date(is_completed: bool, stamp: Any, zone: Optional[tzinfo] = None) -> Optional[date]:
    """None for an open task; the stamp's date for a completed one, else the unknown-day marker."""
    if not is_completed:
        return None
    return date_of(stamp, zone) or UNKNOWN_COMPLETION_DATE


def is_real_completion(day: Optional[date]) -> bool:
    return day is not None and day != UNKNOWN_COMPLETION_DATE


def local_midnight_utc(day: date, zone: Optional[tzinfo] = None) -> datetime:
    """Midnight of ``day`` in ``zone`` (default: the Mac's) as an aware UTC datetime."""
    return to_aware(datetime(day.year, day.month, day.day), zone).astimezone(timezone.utc)


# -- HTML notes --------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIPPED_TAGS:
            self._skipping += 1
        elif tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).split("\n")]
        return _MANY_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """Plain text of an HTML note body: block ends and ``<br>`` become line breaks, tags vanish."""
    extractor = _TextExtractor()
    extractor.feed(html or "")
    extractor.close()
    return extractor.text()


# -- HTTP ----------------------------------------------------------------------------
def send(http: HttpClient, service: str, method: str, url: str, **kwargs: Any) -> HttpResponse:
    """One request; a transport failure or HTTP error becomes a StoreError naming ``service``."""
    try:
        return http.request(method, url, **kwargs)
    except HttpError as exc:
        raise StoreError(f"{service}: {exc}") from exc


def json_object(response: HttpResponse, service: str, what: str) -> Dict[str, Any]:
    """The JSON object of a successful response; anything else is a StoreError."""
    if not response.ok:
        raise StoreError(f"{service} {what} failed: HTTP {response.status} {response.text[:200].strip()}")
    try:
        payload = response.json()
    except HttpError as exc:
        raise StoreError(f"{service} {what}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StoreError(f"{service} {what}: unexpected response {str(payload)[:80]!r}")
    return payload


def item_id_of(payload: Dict[str, Any], service: str, what: str) -> str:
    item_id = payload.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise StoreError(f"{service} {what}: the response carries no item id")
    return item_id


def text_of(value: Any) -> str:
    """A string field of a JSON payload, CRLF-normalized; anything but text is empty."""
    return value.replace("\r\n", "\n") if isinstance(value, str) else ""


def strings_of(value: Any) -> tuple:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


__all__ = ["Log", "NO_SUBJECT", "parse_stamp", "date_of", "completion_date", "is_real_completion",
           "local_midnight_utc", "html_to_text", "send", "json_object", "item_id_of", "text_of", "strings_of"]
