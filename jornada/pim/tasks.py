"""Tasks Database ⇄ :class:`~jornada.pim.models.Task`."""
from __future__ import annotations

from datetime import date
from typing import List, Optional, Tuple

from ..cedb import CEVT_FILETIME, PropVal, Record
from . import ids
from .common import has_prop, int_of, notes_of, put_categories, put_notes, split_categories, string_of
from .models import Task
from .timeconv import date_to_filetime, filetime_to_date

UNKNOWN_COMPLETION_DATE = date(1970, 1, 1)
_PRIORITY_NAMES = {ids.IMPORTANCE_HIGH: "high", ids.IMPORTANCE_NORMAL: "normal", ids.IMPORTANCE_LOW: "low"}
_PRIORITY_CODES = {name: code for code, name in _PRIORITY_NAMES.items()}


def _date_of(record: Record, prop_id: int) -> Optional[date]:
    value = record.value(prop_id)
    return filetime_to_date(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _completed_of(record: Record) -> Optional[date]:
    prop = record.get(ids.TASK_COMPLETED)
    if prop is None or prop.value in (None, 0, False):
        return None
    if prop.kind == CEVT_FILETIME:
        return filetime_to_date(int(prop.value)) or UNKNOWN_COMPLETION_DATE
    return UNKNOWN_COMPLETION_DATE


def decode(record: Record) -> Task:
    return Task(
        summary=string_of(record, ids.SUBJECT),
        due=_date_of(record, ids.TASK_DUE),
        start=_date_of(record, ids.TASK_START),
        completed=_completed_of(record),
        priority=_PRIORITY_NAMES.get(int_of(record, ids.IMPORTANCE, ids.IMPORTANCE_NORMAL), "normal"),
        notes=notes_of(record),
        categories=split_categories(record.value(ids.CATEGORIES, "")),
        private=int_of(record, ids.SENSITIVITY) == ids.SENSITIVITY_PRIVATE,
    )


def _put_date(props: List[PropVal], prop_id: int, day: Optional[date], existing: Optional[Record]) -> None:
    if day is not None:
        props.append(PropVal.filetime(prop_id, date_to_filetime(day)))
    elif has_prop(existing, prop_id):
        props.append(PropVal.deleted(prop_id, CEVT_FILETIME))


def encode(task: Task, existing: Optional[Record] = None) -> Tuple[PropVal, ...]:
    item = task.normalized()
    props: List[PropVal] = [
        PropVal.string(ids.SUBJECT, item.summary or "(no subject)"),
        PropVal.i4(ids.IMPORTANCE, _PRIORITY_CODES.get(item.priority, ids.IMPORTANCE_NORMAL)),
        PropVal.i2(ids.SENSITIVITY, ids.SENSITIVITY_PRIVATE if item.private else ids.SENSITIVITY_PUBLIC),
    ]
    _put_date(props, ids.TASK_START, item.start, existing)
    _put_date(props, ids.TASK_DUE, item.due, existing)
    completed = None if item.completed is None else (
        item.completed if item.completed != UNKNOWN_COMPLETION_DATE else date.today())
    _put_date(props, ids.TASK_COMPLETED, completed, existing)
    put_notes(props, ids.NOTES, item.notes, existing)
    put_categories(props, item.categories, existing)
    return tuple(props)


def is_read_only(task: Task) -> bool:
    return False
