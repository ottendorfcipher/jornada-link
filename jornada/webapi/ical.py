"""iCalendar (RFC 5545) parsing, serialization and the pieces a PIM sync needs.

Everything is an immutable value: :class:`Property` and :class:`Component` are
frozen dataclasses and every "modifying" method returns a new object.

Value model (see :mod:`jornada.webapi._textprops`): ``Property.value`` holds
plain, unescaped text for text-typed properties (SUMMARY, DESCRIPTION,
LOCATION, X-...), so ``event.value("DESCRIPTION")`` contains real newlines
and commas.  Lists, structured values and non-text values (CATEGORIES,
RRULE, DTSTART, URL, ...) keep their wire form so nothing is lost;
:func:`categories_of` and the date helpers decode those.  :func:`serialize`
escapes exactly what :func:`parse` unescaped.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import _textprops as tp

DEFAULT_PRODID = "-//jornada-link//Jornada Sync//EN"
DateOrDateTime = Union[_dt.date, _dt.datetime]

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z)?$")
_DURATION_RE = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)
_UTC_NAMES = frozenset({"UTC", "GMT", "Z", "ETC/UTC", "ETC/GMT", "UCT", "ZULU"})
_ONE_DAY = _dt.timedelta(days=1)


class IcalError(ValueError):
    """Malformed iCalendar text or value."""


# --- data model ----------------------------------------------------------------

@dataclass(frozen=True)
class Property:
    """One content line: ``group.NAME;PARAM=value:value``.

    ``name`` and parameter names are upper-cased; quoted parameter values are
    stored without quotes and a multi-valued parameter such as
    ``TYPE=WORK,VOICE`` stays one string (see :meth:`param_values`).
    """

    name: str
    value: str
    params: Tuple[Tuple[str, str], ...] = ()
    group: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise IcalError(f"property {self.name!r} value must be a string")
        object.__setattr__(self, "name", str(self.name).strip().upper())
        try:
            object.__setattr__(self, "params", tp.normalize_params(self.params))
        except tp.TextPropError as exc:
            raise IcalError(f"property {self.name!r}: {exc}") from exc
        object.__setattr__(self, "group", self.group or None)

    def param(self, name: str) -> Optional[str]:
        """First value of parameter ``name`` (case-insensitive), or None."""
        return tp.param_of(self.params, name)

    def param_values(self, name: str) -> Tuple[str, ...]:
        """Every value of parameter ``name``, with comma lists split apart."""
        wanted = name.upper()
        return tuple(
            item.strip()
            for key, value in self.params if key == wanted
            for item in value.split(",") if item.strip()
        )

    def with_params(self, params: tp.ParamsLike) -> "Property":
        """Copy with ``params`` replaced."""
        return Property(self.name, self.value, tp.normalize_params(params), self.group)


@dataclass(frozen=True)
class Component:
    """A ``BEGIN:NAME`` ... ``END:NAME`` block with properties and child components."""

    name: str
    properties: Tuple[Property, ...] = ()
    children: Tuple["Component", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name).strip().upper())
        object.__setattr__(self, "properties", tuple(self.properties))
        object.__setattr__(self, "children", tuple(self.children))

    def get(self, name: str) -> Optional[Property]:
        """First property called ``name``, or None."""
        wanted = name.upper()
        return next((p for p in self.properties if p.name == wanted), None)

    def get_all(self, name: str) -> Tuple[Property, ...]:
        wanted = name.upper()
        return tuple(p for p in self.properties if p.name == wanted)

    def value(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """Value of the first property called ``name``, or ``default``."""
        prop = self.get(name)
        return default if prop is None else prop.value

    def find(self, name: str) -> Optional["Component"]:
        """First descendant component called ``name`` (depth-first), or None."""
        return next(iter(self.find_all(name)), None)

    def find_all(self, name: str) -> Tuple["Component", ...]:
        """Every descendant component called ``name``, depth-first."""
        wanted = name.upper()
        return tuple(_walk(self, wanted))

    def with_property(self, prop: Property, replace: bool = True) -> "Component":
        """Copy with ``prop`` added; by default it replaces properties of the same name."""
        if not replace or self.get(prop.name) is None:
            return Component(self.name, self.properties + (prop,), self.children)
        kept = [p for p in self.properties if p.name != prop.name]
        first = next(i for i, p in enumerate(self.properties) if p.name == prop.name)
        kept.insert(min(first, len(kept)), prop)
        return Component(self.name, tuple(kept), self.children)

    def without_property(self, name: str) -> "Component":
        """Copy without any property called ``name``."""
        wanted = name.upper()
        return Component(self.name, tuple(p for p in self.properties if p.name != wanted), self.children)

    def with_child(self, child: "Component") -> "Component":
        """Copy with ``child`` appended."""
        return Component(self.name, self.properties, self.children + (child,))


def _walk(component: Component, wanted: str) -> Iterator[Component]:
    for child in component.children:
        if child.name == wanted:
            yield child
        yield from _walk(child, wanted)


# --- parse / serialize -----------------------------------------------------------

class _Frame:
    """Mutable scratch space used only while :func:`parse` builds one component."""

    __slots__ = ("name", "properties", "children")

    def __init__(self, name: str) -> None:
        self.name = name
        self.properties: List[Property] = []
        self.children: List[Component] = []

    def build(self) -> Component:
        return Component(self.name, tuple(self.properties), tuple(self.children))


def parse(text: str) -> Component:
    """Parse iCalendar text into a :class:`Component` tree.

    Tolerates CRLF/LF/CR line ends, folded lines, blank lines and any component
    names.  Text without a VCALENDAR wrapper yields the single top-level
    component, or a synthetic VCALENDAR when there are several.  Raises
    :class:`IcalError` on unbalanced BEGIN/END or a line without a colon.
    """
    stack: List[_Frame] = []
    top = _Frame("VCALENDAR")
    for line in tp.unfold(text):
        try:
            parsed = tp.parse_line(line)
        except tp.TextPropError as exc:
            raise IcalError(str(exc)) from exc
        if parsed.name == "BEGIN":
            stack.append(_Frame(parsed.value.strip().upper()))
        elif parsed.name == "END":
            finished = _pop_frame(stack, parsed.value).build()
            (stack[-1] if stack else top).children.append(finished)
        else:
            (stack[-1] if stack else top).properties.append(_property_from_line(parsed))
    if stack:
        raise IcalError(f"unterminated BEGIN:{stack[-1].name}")
    return _top_level(top)


def _pop_frame(stack: List[_Frame], name: str) -> _Frame:
    wanted = name.strip().upper()
    if not stack:
        raise IcalError(f"END:{wanted} without a matching BEGIN")
    if stack[-1].name != wanted:
        raise IcalError(f"END:{wanted} closes BEGIN:{stack[-1].name}")
    return stack.pop()


def _property_from_line(parsed: tp.ContentLine) -> Property:
    value = tp.decode_value(parsed.name, parsed.params, parsed.value)
    return Property(parsed.name, value, parsed.params, parsed.group)


def _top_level(top: _Frame) -> Component:
    if not top.children and not top.properties:
        raise IcalError("no iCalendar content found")
    if len(top.children) == 1 and not top.properties:
        return top.children[0]
    defaults = [Property("VERSION", "2.0"), Property("PRODID", DEFAULT_PRODID)]
    present = {p.name for p in top.properties}
    props = [p for p in defaults if p.name not in present] + top.properties
    return Component("VCALENDAR", tuple(props), tuple(top.children))


def serialize(component: Component) -> str:
    """Render a component tree as iCalendar text (CRLF, folded at 75 octets)."""
    return tp.CRLF.join(_lines(component)) + tp.CRLF


def _lines(component: Component) -> Iterator[str]:
    yield f"BEGIN:{component.name}"
    for prop in component.properties:
        wire = tp.encode_value(prop.name, prop.params, prop.value)
        yield tp.fold(tp.format_line(prop.group, prop.name, prop.params, wire))
    for child in component.children:
        yield from _lines(child)
    yield f"END:{component.name}"


# --- dates and times ---------------------------------------------------------------

def parse_datetime(prop: Property) -> DateOrDateTime:
    """Decode a DATE or DATE-TIME property (see :func:`parse_datetime_value`)."""
    return parse_datetime_value(prop.value, prop.params)


def parse_datetime_value(value: str, params: tp.ParamsLike = None) -> DateOrDateTime:
    """Decode an iCalendar date/time value.

    ``VALUE=DATE`` or an 8-digit value gives a :class:`datetime.date`;
    ``YYYYMMDDTHHMMSS`` a naive (floating) datetime; a ``Z`` suffix an aware
    UTC datetime; a ``TZID`` parameter an aware datetime when the zone is
    known to :mod:`zoneinfo`, else a naive one.  Raises :class:`IcalError`.
    """
    normalized = tp.normalize_params(params)
    text = value.strip()
    declared = (tp.param_of(normalized, "VALUE") or "").upper()
    if declared == "DATE":
        return _parse_date(text[:8])
    if declared != "DATE-TIME" and _DATE_RE.match(text):
        return _parse_date(text)
    match = _DATETIME_RE.match(text)
    if not match:
        raise IcalError(f"malformed date-time value {text!r}")
    naive = _build_datetime(match.groups()[:6], text)
    if match.group(7):
        return naive.replace(tzinfo=_dt.timezone.utc)
    tzid = tp.param_of(normalized, "TZID")
    zone = resolve_zone(tzid) if tzid else None
    return naive.replace(tzinfo=zone) if zone is not None else naive


def _parse_date(text: str) -> _dt.date:
    match = _DATE_RE.match(text)
    if not match:
        raise IcalError(f"malformed date value {text!r}")
    try:
        return _dt.date(*(int(part) for part in match.groups()))
    except ValueError as exc:
        raise IcalError(f"invalid date {text!r}: {exc}") from exc


def _build_datetime(parts: Sequence[Optional[str]], text: str) -> _dt.datetime:
    numbers = [int(part) if part else 0 for part in parts]
    try:
        return _dt.datetime(*numbers)
    except ValueError as exc:
        raise IcalError(f"invalid date-time {text!r}: {exc}") from exc


def resolve_zone(tzid: str) -> Optional[_dt.tzinfo]:
    """A tzinfo for ``tzid`` when :mod:`zoneinfo` knows it (also for ``/prefix/Area/City`` spellings)."""
    cleaned = tzid.strip().strip("/")
    if cleaned.upper() in _UTC_NAMES:
        return _dt.timezone.utc
    segments = cleaned.split("/")
    candidates = [cleaned] + ["/".join(segments[-n:]) for n in (3, 2) if len(segments) > n]
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return None


def format_date(day: _dt.date) -> str:
    """``YYYYMMDD``."""
    return day.strftime("%Y%m%d")


def format_datetime(moment: _dt.datetime) -> str:
    """Aware datetimes become UTC with a ``Z`` suffix; naive ones stay floating."""
    if moment.tzinfo is None:
        return moment.strftime("%Y%m%dT%H%M%S")
    return moment.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def datetime_property(name: str, value: DateOrDateTime, tzid: Optional[str] = None) -> Property:
    """Build a date/time property: ``VALUE=DATE`` for dates, ``TZID=`` local time when given."""
    if isinstance(value, _dt.datetime):
        if tzid:
            return Property(name, _local_text(value, tzid), (("TZID", tzid),))
        return Property(name, format_datetime(value))
    if isinstance(value, _dt.date):
        return Property(name, format_date(value), (("VALUE", "DATE"),))
    raise IcalError(f"{name} needs a date or datetime, got {type(value).__name__}")


def _local_text(moment: _dt.datetime, tzid: str) -> str:
    zone = resolve_zone(tzid)
    if moment.tzinfo is not None and zone is not None:
        moment = moment.astimezone(zone)
    return moment.strftime("%Y%m%dT%H%M%S")


def parse_duration(text: str) -> _dt.timedelta:
    """Decode ``[+-]PnW`` / ``[+-]PnDTnHnMnS`` (any subset, e.g. ``-PT15M``)."""
    match = _DURATION_RE.match(text.strip().upper())
    if not match or not any(match.groups()[1:]):
        raise IcalError(f"malformed duration {text!r}")
    sign, weeks, days, hours, minutes, seconds = match.groups()
    delta = _dt.timedelta(
        weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
        minutes=int(minutes or 0), seconds=int(seconds or 0),
    )
    return -delta if sign == "-" else delta


def format_duration(delta: _dt.timedelta) -> str:
    """Encode a timedelta; negative values get the ``-P`` prefix, zero is ``PT0S``."""
    sign = "-" if delta < _dt.timedelta(0) else ""
    total = int(abs(delta).total_seconds())
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days and days % 7 == 0 and not (hours or minutes or seconds):
        return f"{sign}P{days // 7}W"
    clock = "".join(f"{n}{u}" for n, u in ((hours, "H"), (minutes, "M"), (seconds, "S")) if n)
    body = (f"{days}D" if days else "") + (f"T{clock}" if clock else "")
    return f"{sign}P{body or 'T0S'}"


# --- builders -------------------------------------------------------------------------

def vcalendar(
    *components: Component,
    prodid: str = DEFAULT_PRODID,
    method: Optional[str] = None,
) -> Component:
    """A VCALENDAR wrapping ``components`` (VERSION 2.0, PRODID, CALSCALE, optional METHOD)."""
    props = [Property("VERSION", "2.0"), Property("PRODID", prodid), Property("CALSCALE", "GREGORIAN")]
    if method:
        props.append(Property("METHOD", method.upper()))
    return Component("VCALENDAR", tuple(props), tuple(components))


def vevent(
    uid: str,
    summary: str,
    start: DateOrDateTime,
    end: Optional[DateOrDateTime],
    all_day: bool = False,
    location: Optional[str] = None,
    description: Optional[str] = None,
    categories: Sequence[str] = (),
    alarm_minutes_before: Optional[int] = None,
    transparent: Optional[bool] = None,
    dtstamp: Optional[_dt.datetime] = None,
    extra: Sequence[Property] = (),
) -> Component:
    """Build a VEVENT.

    With ``all_day`` both ends are written as ``VALUE=DATE`` and DTEND is
    exclusive (the day after the last day): pass ``end`` as that exclusive
    date, or as a datetime inside the last day (e.g. 23:59) which is rounded
    up; ``end`` equal to ``start`` (or None) gives a one-day event.  An alarm
    becomes a VALARM child (``ACTION:DISPLAY``, ``TRIGGER:-PT15M`` style).
    """
    _require(uid, "uid")
    props = [Property("UID", uid), _dtstamp(dtstamp), Property("SUMMARY", summary)]
    props += _span_properties(start, end, all_day)
    props += _text_properties(("LOCATION", location), ("DESCRIPTION", description))
    props += _categories_properties(categories)
    if transparent is not None:
        props.append(Property("TRANSP", "TRANSPARENT" if transparent else "OPAQUE"))
    props += list(extra)
    children = () if alarm_minutes_before is None else (_display_alarm(alarm_minutes_before, summary),)
    return Component("VEVENT", tuple(props), children)


def vtodo(
    uid: str,
    summary: str,
    due: Optional[DateOrDateTime] = None,
    start: Optional[DateOrDateTime] = None,
    completed: Optional[_dt.datetime] = None,
    priority: Optional[int] = None,
    description: Optional[str] = None,
    categories: Sequence[str] = (),
    status: Optional[str] = None,
    percent_complete: Optional[int] = None,
    dtstamp: Optional[_dt.datetime] = None,
    extra: Sequence[Property] = (),
) -> Component:
    """Build a VTODO; dates given as :class:`datetime.date` are written with ``VALUE=DATE``."""
    _require(uid, "uid")
    props = [Property("UID", uid), _dtstamp(dtstamp), Property("SUMMARY", summary)]
    for name, when in (("DTSTART", start), ("DUE", due), ("COMPLETED", completed)):
        if when is not None:
            props.append(datetime_property(name, when))
    if priority is not None:
        props.append(Property("PRIORITY", str(_bounded(priority, 0, 9, "priority"))))
    if status:
        props.append(Property("STATUS", status.strip().upper()))
    if percent_complete is not None:
        props.append(Property("PERCENT-COMPLETE", str(_bounded(percent_complete, 0, 100, "percent_complete"))))
    props += _text_properties(("DESCRIPTION", description))
    props += _categories_properties(categories)
    props += list(extra)
    return Component("VTODO", tuple(props), ())


def _require(value: str, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise IcalError(f"{what} must be a non-empty string")


def _bounded(value: int, low: int, high: int, what: str) -> int:
    if not isinstance(value, int) or not low <= value <= high:
        raise IcalError(f"{what} must be an integer between {low} and {high}, got {value!r}")
    return value


def _dtstamp(moment: Optional[_dt.datetime]) -> Property:
    stamp = moment if moment is not None else _dt.datetime.now(_dt.timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return Property("DTSTAMP", format_datetime(stamp))


def _span_properties(start: DateOrDateTime, end: Optional[DateOrDateTime], all_day: bool) -> List[Property]:
    if not all_day:
        props = [datetime_property("DTSTART", start)]
        return props + ([datetime_property("DTEND", end)] if end is not None else [])
    first = _as_date(start, inclusive=True)
    last_exclusive = first + _ONE_DAY if end is None else _as_date(end, inclusive=False)
    if last_exclusive <= first:
        last_exclusive = first + _ONE_DAY
    return [datetime_property("DTSTART", first), datetime_property("DTEND", last_exclusive)]


def _as_date(value: DateOrDateTime, inclusive: bool) -> _dt.date:
    if isinstance(value, _dt.datetime):
        day = value.date()
        # An end time inside the day (e.g. 23:59) means that day is included.
        return day if inclusive or value.time() == _dt.time(0) else day + _ONE_DAY
    if isinstance(value, _dt.date):
        return value
    raise IcalError(f"all-day events need dates, got {type(value).__name__}")


def _text_properties(*pairs: Tuple[str, Optional[str]]) -> List[Property]:
    return [Property(name, text) for name, text in pairs if text]


def _categories_properties(categories: Union[str, Sequence[str]]) -> List[Property]:
    items = [categories] if isinstance(categories, str) else list(categories)
    cleaned = [item.strip() for item in items if isinstance(item, str) and item.strip()]
    if not cleaned:
        return []
    return [Property("CATEGORIES", ",".join(tp.escape(item) for item in cleaned))]


def _display_alarm(minutes_before: int, summary: str) -> Component:
    if not isinstance(minutes_before, int) or minutes_before < 0:
        raise IcalError(f"alarm_minutes_before must be a non-negative integer, got {minutes_before!r}")
    trigger = format_duration(-_dt.timedelta(minutes=minutes_before))
    props = (Property("ACTION", "DISPLAY"), Property("TRIGGER", trigger), Property("DESCRIPTION", summary))
    return Component("VALARM", props, ())


# --- readers ----------------------------------------------------------------------------

def alarm_minutes_before(component: Component) -> Optional[int]:
    """Minutes before the start of the first relative, negative VALARM trigger.

    Absolute (``VALUE=DATE-TIME``) triggers and ``ACTION:NONE`` placeholders
    (iCloud) are ignored; a zero or positive relative trigger counts as 0.
    """
    fallback: Optional[int] = None
    for alarm in component.find_all("VALARM"):
        delta = _relative_trigger(alarm)
        if delta is None:
            continue
        if delta < _dt.timedelta(0):
            return round(-delta.total_seconds() / 60)
        fallback = 0
    return fallback


def _relative_trigger(alarm: Component) -> Optional[_dt.timedelta]:
    if (alarm.value("ACTION") or "").upper() == "NONE":
        return None
    trigger = alarm.get("TRIGGER")
    if trigger is None or (trigger.param("VALUE") or "").upper() == "DATE-TIME":
        return None
    try:
        return parse_duration(trigger.value)
    except IcalError:
        return None


def categories_of(component: Component) -> Tuple[str, ...]:
    """Every category of every CATEGORIES property, unescaped and stripped."""
    return tuple(
        tp.unescape(part).strip()
        for prop in component.get_all("CATEGORIES")
        for part in tp.split_unescaped(prop.value, ",")
        if tp.unescape(part).strip()
    )


def is_all_day(component: Component) -> bool:
    """True when DTSTART is a DATE (``VALUE=DATE`` or an 8-digit value)."""
    start = component.get("DTSTART")
    return start is not None and _is_date(start)


def _is_date(prop: Property) -> bool:
    return (prop.param("VALUE") or "").upper() == "DATE" or bool(_DATE_RE.match(prop.value.strip()))


def event_span(component: Component) -> Tuple[DateOrDateTime, DateOrDateTime]:
    """``(start, end)`` from DTSTART and DTEND, else DTSTART+DURATION, else DTSTART.

    All-day events without DTEND end the next day (DTEND is exclusive).
    Raises :class:`IcalError` when there is no DTSTART.
    """
    start_prop = component.get("DTSTART")
    if start_prop is None:
        raise IcalError(f"{component.name} has no DTSTART")
    start = parse_datetime(start_prop)
    end_prop = component.get("DTEND")
    if end_prop is not None:
        return start, parse_datetime(end_prop)
    duration = component.get("DURATION")
    if duration is not None:
        return start, start + parse_duration(duration.value)
    if not isinstance(start, _dt.datetime):
        return start, start + _ONE_DAY
    return start, start
