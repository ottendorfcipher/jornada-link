"""Appointment ⇄ VEVENT, the mapping behind the CalDAV backend.

Timed events are written as UTC (``Z``) instants and read back into the Mac's
wall-clock; all-day events travel as ``VALUE=DATE`` with an exclusive DTEND;
floating (zone-less) incoming times are kept as they are. Busy status uses
TRANSP plus Outlook's ``X-MICROSOFT-CDO-BUSYSTATUS`` so all four device states
survive a round trip: TRANSP decides free versus not free, the X property
refines "not free" into tentative / busy / out of office.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from ...pim.models import Appointment
from ...pim.timeconv import midnight
from ...webapi import ical
from .common import all_day_end, local_aware, local_wall_clock

BUSY_STATUS_PROPERTY = "X-MICROSOFT-CDO-BUSYSTATUS"
BUSY_TO_OUTLOOK = {"free": "FREE", "tentative": "TENTATIVE", "busy": "BUSY", "out_of_office": "OOF"}
OUTLOOK_TO_BUSY = {"FREE": "free", "TENTATIVE": "tentative", "BUSY": "busy", "OOF": "out_of_office",
                   "WORKINGELSEWHERE": "busy"}
PRIVATE_CLASSES = ("PRIVATE", "CONFIDENTIAL")
RECURRENCE_PROPERTIES = ("RRULE", "RDATE", "RECURRENCE-ID")


# -- writing ---------------------------------------------------------------------
def to_vevent(appointment: Appointment, uid: str, dtstamp: Optional[datetime] = None) -> ical.Component:
    """A VEVENT for ``appointment`` under ``uid`` (raises :class:`ical.IcalError` on an empty uid)."""
    appt = appointment.normalized()
    if appt.all_day:
        start, end = midnight(appt.start).date(), all_day_end(appt.start, appt.end).date()
    else:
        start, end = local_aware(appt.start), local_aware(max(appt.end, appt.start))
    extra = [ical.Property(BUSY_STATUS_PROPERTY, BUSY_TO_OUTLOOK.get(appt.busy_status, "BUSY"))]
    if appt.private:
        extra.append(ical.Property("CLASS", "PRIVATE"))
    reminder = None if appt.reminder_minutes is None else max(int(appt.reminder_minutes), 0)
    return ical.vevent(uid, appt.summary, start, end, all_day=appt.all_day, location=appt.location or None,
                       description=appt.notes or None, categories=appt.categories, alarm_minutes_before=reminder,
                       transparent=appt.busy_status == "free", dtstamp=dtstamp, extra=extra)


# -- reading ---------------------------------------------------------------------
def from_vevent(component: ical.Component) -> Appointment:
    """The Appointment described by a VEVENT (raises :class:`ical.IcalError` when it has no DTSTART)."""
    all_day = ical.is_all_day(component)
    first, last = ical.event_span(component)
    start, end = _all_day_span(first, last) if all_day else _timed_span(first, last)
    return Appointment(
        summary=component.value("SUMMARY") or "",
        start=start,
        end=end,
        all_day=all_day,
        location=component.value("LOCATION") or "",
        notes=component.value("DESCRIPTION") or "",
        categories=ical.categories_of(component),
        busy_status=busy_status_of(component),
        private=is_private(component),
        reminder_minutes=ical.alarm_minutes_before(component),
        recurring=is_recurring(component),
        uid=component.value("UID") or "",
    )


def _wall_clock(value: ical.DateOrDateTime) -> datetime:
    """An iCalendar value → naive local: aware instants are converted, floating ones kept, dates → midnight."""
    return local_wall_clock(value) if isinstance(value, datetime) else midnight(value)


def _timed_span(first: ical.DateOrDateTime, last: ical.DateOrDateTime) -> Tuple[datetime, datetime]:
    start = _wall_clock(first)
    return start, max(_wall_clock(last), start)


def _all_day_span(first: ical.DateOrDateTime, last: ical.DateOrDateTime) -> Tuple[datetime, datetime]:
    start = midnight(_wall_clock(first))
    return start, all_day_end(start, _wall_clock(last))


def busy_status_of(component: ical.Component) -> str:
    """TRANSP decides free / not free; ``X-MICROSOFT-CDO-BUSYSTATUS`` refines the latter."""
    transp = (component.value("TRANSP") or "").strip().upper()
    outlook = OUTLOOK_TO_BUSY.get((component.value(BUSY_STATUS_PROPERTY) or "").strip().upper())
    if transp == "TRANSPARENT" or (not transp and outlook == "free"):
        return "free"
    return outlook if outlook in ("tentative", "out_of_office") else "busy"


def is_private(component: ical.Component) -> bool:
    return (component.value("CLASS") or "").strip().upper() in PRIVATE_CLASSES


def is_recurring(component: ical.Component) -> bool:
    """A series master (RRULE / RDATE) or one of its overrides (RECURRENCE-ID)."""
    return any(component.get(name) is not None for name in RECURRENCE_PROPERTIES)


def master_vevent(component: ical.Component) -> Optional[ical.Component]:
    """The plain event or series master of a parsed object: the first VEVENT without RECURRENCE-ID."""
    events = (component,) if component.name == "VEVENT" else component.find_all("VEVENT")
    return next((event for event in events if event.get("RECURRENCE-ID") is None), None)


__all__ = ["to_vevent", "from_vevent", "busy_status_of", "is_private", "is_recurring", "master_vevent",
           "BUSY_STATUS_PROPERTY", "BUSY_TO_OUTLOOK", "OUTLOOK_TO_BUSY"]
