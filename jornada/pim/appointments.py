"""Appointments Database ⇄ :class:`~jornada.pim.models.Appointment`.

Conventions follow SynCE's librra, which established them against devices:
an all-day event starts at midnight with type 1 and a duration of
``(days - 1) * 1440 + 1`` minutes; timed events carry their length in minutes.
Recurring appointments are decoded (``recurring=True``) but never rewritten.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from ..cedb import CEVT_FILETIME, CEVT_I4, PropVal, Record
from . import ids
from .common import has_prop, int_of, notes_of, put_categories, put_notes, put_string, split_categories, string_of
from .models import Appointment
from .timeconv import filetime_to_naive, midnight, naive_to_filetime

_BUSY_NAMES = {ids.BUSY_FREE: "free", ids.BUSY_TENTATIVE: "tentative",
               ids.BUSY_BUSY: "busy", ids.BUSY_OUT_OF_OFFICE: "out_of_office"}
_BUSY_CODES = {name: code for code, name in _BUSY_NAMES.items()}
MINUTES_PER_DAY = 1440
_FALLBACK_START = datetime(1970, 1, 1)


def days_from_duration(minutes: int) -> int:
    """Length in days of an all-day event from its stored duration (tolerant of both conventions)."""
    if minutes <= 1:
        return 1
    if minutes % MINUTES_PER_DAY == 0:
        return minutes // MINUTES_PER_DAY
    if (minutes - 1) % MINUTES_PER_DAY == 0:
        return (minutes - 1) // MINUTES_PER_DAY + 1
    return (minutes + MINUTES_PER_DAY - 1) // MINUTES_PER_DAY


def duration_for_days(days: int) -> int:
    return (max(days, 1) - 1) * MINUTES_PER_DAY + 1


def decode(record: Record) -> Appointment:
    start = filetime_to_naive(int_of(record, ids.APPT_START)) or _FALLBACK_START
    duration = int_of(record, ids.APPT_DURATION)
    all_day = int_of(record, ids.APPT_TYPE, ids.APPT_TYPE_NORMAL) == ids.APPT_TYPE_ALL_DAY
    if all_day:
        start = midnight(start)
        end = start + timedelta(days=days_from_duration(duration))
    else:
        end = start + timedelta(minutes=max(duration, 0))
    reminder = int_of(record, ids.REMINDER_MINUTES) if int_of(record, ids.REMINDER_ENABLED) else None
    recurring = (int_of(record, ids.APPT_OCCURRENCE) == ids.OCCURRENCE_REPEATED
                 or record.get(ids.APPT_RECURRENCE_PATTERN) is not None)
    return Appointment(
        summary=string_of(record, ids.SUBJECT),
        start=start,
        end=end,
        all_day=all_day,
        location=string_of(record, ids.APPT_LOCATION),
        notes=notes_of(record),
        categories=split_categories(record.value(ids.CATEGORIES, "")),
        busy_status=_BUSY_NAMES.get(int_of(record, ids.APPT_BUSY_STATUS, ids.BUSY_BUSY), "busy"),
        private=int_of(record, ids.SENSITIVITY) == ids.SENSITIVITY_PRIVATE,
        reminder_minutes=reminder,
        recurring=recurring,
    )


def encode(appointment: Appointment, existing: Optional[Record] = None) -> Tuple[PropVal, ...]:
    """Properties for CeWriteRecordProps; ``existing`` lets cleared fields be deleted."""
    appt = appointment.normalized()
    props: List[PropVal] = [PropVal.string(ids.SUBJECT, appt.summary or "(no subject)")]
    if appt.all_day:
        start = midnight(appt.start)
        days = max((midnight(appt.end) - start).days, 1)
        duration, kind = duration_for_days(days), ids.APPT_TYPE_ALL_DAY
    else:
        start = appt.start
        duration = max(int(round((appt.end - appt.start).total_seconds() / 60)), 0)
        kind = ids.APPT_TYPE_NORMAL
    props += [
        PropVal.filetime(ids.APPT_START, naive_to_filetime(start)),
        PropVal.i4(ids.APPT_DURATION, duration),
        PropVal.i4(ids.APPT_TYPE, kind),
        PropVal.i2(ids.APPT_OCCURRENCE, ids.OCCURRENCE_ONCE),
        PropVal.i2(ids.APPT_BUSY_STATUS, _BUSY_CODES.get(appt.busy_status, ids.BUSY_BUSY)),
        PropVal.i2(ids.SENSITIVITY, ids.SENSITIVITY_PRIVATE if appt.private else ids.SENSITIVITY_PUBLIC),
        PropVal.i4(ids.UNKNOWN_0002, 0),
    ]
    put_string(props, ids.APPT_LOCATION, appt.location, existing)
    put_notes(props, ids.NOTES, appt.notes, existing)
    put_categories(props, appt.categories, existing)
    props += _reminder_props(appt.reminder_minutes, existing)
    return tuple(props)


def _reminder_props(minutes: Optional[int], existing: Optional[Record]) -> List[PropVal]:
    if minutes is None:
        props = [PropVal.i2(ids.REMINDER_ENABLED, 0)]
        if has_prop(existing, ids.REMINDER_MINUTES):
            props.append(PropVal.deleted(ids.REMINDER_MINUTES, CEVT_I4))
        return props
    return [
        PropVal.i2(ids.REMINDER_ENABLED, 1),
        PropVal.i4(ids.REMINDER_MINUTES, max(int(minutes), 0)),
        PropVal.i4(ids.REMINDER_OPTIONS, ids.DEFAULT_REMINDER_OPTIONS),
        PropVal.string(ids.REMINDER_SOUND, ids.DEFAULT_REMINDER_SOUND),
    ]


def is_read_only(appointment: Appointment) -> bool:
    """Recurring device appointments are left alone: the pattern blob is not modelled."""
    return appointment.recurring


__all__ = ["decode", "encode", "is_read_only", "days_from_duration", "duration_for_days", "CEVT_FILETIME"]
