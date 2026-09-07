"""VTODO ⇄ :class:`~jornada.pim.models.Task`.

Reading is tolerant: a task counts as completed with a COMPLETED stamp, a
``STATUS:COMPLETED`` or ``PERCENT-COMPLETE:100`` (the unknown-day marker when
there is no stamp); DUE/DTSTART may be DATE or DATE-TIME values (an aware
date-time is read in the Mac's zone). Writing keeps whatever properties and
alarms of the original VTODO this mapping does not own.
"""
from __future__ import annotations

from datetime import date, datetime, tzinfo
from typing import Optional, Sequence, Tuple

from ...pim.models import Task
from ...pim.tasks import UNKNOWN_COMPLETION_DATE
from ...pim.timeconv import to_wall_clock
from ...webapi import ical
from ...webapi.ical import Component, IcalError, Property
from .common import NO_SUBJECT, is_real_completion, local_midnight_utc

PRIORITY_CODES = {"high": 1, "low": 9}
OWNED_PROPERTIES = frozenset({
    "UID", "DTSTAMP", "SUMMARY", "DTSTART", "DUE", "COMPLETED", "PRIORITY", "STATUS", "PERCENT-COMPLETE",
    "DESCRIPTION", "CATEGORIES", "CLASS", "LAST-MODIFIED",
})


# -- reading -----------------------------------------------------------------------
def date_of_property(prop: Optional[Property], zone: Optional[tzinfo] = None) -> Optional[date]:
    """A DATE or DATE-TIME property → date (aware stamps read in ``zone``, default local)."""
    if prop is None:
        return None
    try:
        value = ical.parse_datetime(prop)
    except IcalError:
        return None
    if isinstance(value, datetime):
        return to_wall_clock(value, zone).date()
    return value


def priority_name(value: Optional[str]) -> str:
    """RFC 5545 PRIORITY → high (1-4), normal (0, 5, absent) or low (6-9)."""
    try:
        code = int((value or "0").strip())
    except ValueError:
        return "normal"
    if 1 <= code <= 4:
        return "high"
    if 6 <= code <= 9:
        return "low"
    return "normal"


def completed_of(component: Component, zone: Optional[tzinfo] = None) -> Optional[date]:
    stamp = component.get("COMPLETED")
    day = date_of_property(stamp, zone)
    if day is not None:
        return day
    status = (component.value("STATUS") or "").strip().upper()
    if stamp is not None or status == "COMPLETED" or _percent(component) == 100:
        return UNKNOWN_COMPLETION_DATE
    return None


def _percent(component: Component) -> Optional[int]:
    try:
        return int((component.value("PERCENT-COMPLETE") or "").strip())
    except ValueError:
        return None


def from_vtodo(component: Component, zone: Optional[tzinfo] = None) -> Task:
    """A VTODO component → Task."""
    if component.name != "VTODO":
        raise IcalError(f"expected a VTODO, got {component.name}")
    return Task(
        summary=component.value("SUMMARY") or "",
        due=date_of_property(component.get("DUE"), zone),
        start=date_of_property(component.get("DTSTART"), zone),
        completed=completed_of(component, zone),
        priority=priority_name(component.value("PRIORITY")),
        notes=component.value("DESCRIPTION") or "",
        categories=ical.categories_of(component),
        private=(component.value("CLASS") or "").strip().upper() == "PRIVATE",
        uid=component.value("UID") or "",
    )


# -- writing -------------------------------------------------------------------------
def to_vtodo(task: Task, uid: str, base: Optional[Component] = None, zone: Optional[tzinfo] = None,
             dtstamp: Optional[datetime] = None) -> Component:
    """Task → VTODO with ``uid``; properties and alarms of ``base`` this mapping does not own are kept."""
    item = task.normalized()
    completed = local_midnight_utc(item.completed, zone) if is_real_completion(item.completed) else None
    todo = ical.vtodo(
        uid, item.summary or NO_SUBJECT, due=item.due, start=item.start, completed=completed,
        priority=PRIORITY_CODES.get(item.priority), description=item.notes or None,
        categories=item.categories, status="COMPLETED" if item.is_completed else "NEEDS-ACTION",
        percent_complete=100 if item.is_completed else None, dtstamp=dtstamp,
        extra=_class_property(item.private) + kept_properties(base),
    )
    return Component("VTODO", todo.properties, base.children if base is not None else ())


def _class_property(private: bool) -> Tuple[Property, ...]:
    return (Property("CLASS", "PRIVATE"),) if private else ()


def kept_properties(base: Optional[Component]) -> Tuple[Property, ...]:
    if base is None:
        return ()
    return tuple(p for p in base.properties if p.name not in OWNED_PROPERTIES)


def calendar_with(todo: Component, original: Optional[Component] = None) -> Component:
    """A VCALENDAR holding ``todo`` plus the non-VTODO components (time zones) of ``original``."""
    others: Sequence[Component] = () if original is None else tuple(c for c in original.children if c.name != "VTODO")
    return ical.vcalendar(*others, todo)


def first_vtodo(calendar: Component) -> Optional[Component]:
    """The VTODO of a parsed calendar object (the component itself when unwrapped), or None."""
    if calendar.name == "VTODO":
        return calendar
    return calendar.find("VTODO")


__all__ = ["from_vtodo", "to_vtodo", "calendar_with", "first_vtodo", "kept_properties", "date_of_property",
           "priority_name", "completed_of", "OWNED_PROPERTIES", "PRIORITY_CODES"]
