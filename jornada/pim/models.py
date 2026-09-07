"""Neutral records shared by the device codecs and every modern backend.

All records are frozen dataclasses with JSON round-trips (`to_dict` /
`from_dict`), a content ``fingerprint()`` for change detection and a
``match_key()`` used to pair up records that were never synced before.
Times are naive wall-clock values (see :mod:`jornada.pim.timeconv`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from typing import Any, ClassVar, Dict, Optional, Tuple, Type, TypeVar

from .timeconv import parse_iso

T = TypeVar("T", bound="Record")

BUSY_STATES = ("free", "tentative", "busy", "out_of_office")
PRIORITIES = ("high", "normal", "low")
PHONE_KINDS = ("work", "work2", "home", "home2", "mobile", "work_fax", "home_fax",
               "pager", "car", "radio", "assistant")
ADDRESS_KINDS = ("home", "work", "other")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _clean(text: Optional[str]) -> str:
    return (text or "").strip()


def _clean_list(values: Any) -> Tuple[str, ...]:
    seen = []
    for value in values or ():
        cleaned = _clean(value)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


@dataclass(frozen=True)
class Record:
    """Common behaviour: JSON conversion and hashing over the normalized content."""

    # Fields that identify or locate a record rather than describe its content.
    HASH_EXCLUDE: ClassVar[Tuple[str, ...]] = ("uid",)

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: _jsonable(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
        names = {f.name for f in fields(cls)}
        return cls(**{k: cls._revive(k, v) for k, v in data.items() if k in names})

    @classmethod
    def _revive(cls, name: str, value: Any) -> Any:
        return value

    def normalized(self: T) -> T:
        return self

    def fingerprint(self) -> str:
        content = self.normalized().to_dict()
        for name in self.HASH_EXCLUDE:
            content.pop(name, None)
        canonical = json.dumps(content, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()

    def match_key(self) -> str:
        return self.fingerprint()


@dataclass(frozen=True)
class Appointment(Record):
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    notes: str = ""
    categories: Tuple[str, ...] = ()
    busy_status: str = "busy"
    private: bool = False
    reminder_minutes: Optional[int] = None
    recurring: bool = False
    uid: str = ""

    @classmethod
    def _revive(cls, name: str, value: Any) -> Any:
        if name in ("start", "end") and isinstance(value, str):
            moment = parse_iso(value)
            return moment if isinstance(moment, datetime) else datetime(moment.year, moment.month, moment.day)
        if name == "categories":
            return tuple(value or ())
        return value

    def normalized(self) -> "Appointment":
        busy = self.busy_status if self.busy_status in BUSY_STATES else "busy"
        return replace(self, summary=_clean(self.summary), location=_clean(self.location),
                       notes=self.notes.replace("\r\n", "\n").strip(), categories=_clean_list(self.categories),
                       busy_status=busy, start=self.start.replace(microsecond=0),
                       end=self.end.replace(microsecond=0))

    def match_key(self) -> str:
        me = self.normalized()
        return f"appt|{me.summary.casefold()}|{me.start.isoformat(timespec='minutes')}"


@dataclass(frozen=True)
class Task(Record):
    summary: str
    due: Optional[date] = None
    start: Optional[date] = None
    completed: Optional[date] = None
    priority: str = "normal"
    notes: str = ""
    categories: Tuple[str, ...] = ()
    private: bool = False
    uid: str = ""

    @classmethod
    def _revive(cls, name: str, value: Any) -> Any:
        if name in ("due", "start", "completed") and isinstance(value, str):
            moment = parse_iso(value)
            return moment.date() if isinstance(moment, datetime) else moment
        if name == "categories":
            return tuple(value or ())
        return value

    @property
    def is_completed(self) -> bool:
        return self.completed is not None

    def normalized(self) -> "Task":
        priority = self.priority if self.priority in PRIORITIES else "normal"
        return replace(self, summary=_clean(self.summary), notes=self.notes.replace("\r\n", "\n").strip(),
                       categories=_clean_list(self.categories), priority=priority)

    def match_key(self) -> str:
        me = self.normalized()
        return f"task|{me.summary.casefold()}|{me.due.isoformat() if me.due else ''}"


@dataclass(frozen=True)
class Address(Record):
    kind: str = "home"
    street: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""

    @property
    def is_empty(self) -> bool:
        return not any((self.street, self.city, self.state, self.postal_code, self.country))


@dataclass(frozen=True)
class Contact(Record):
    first_name: str = ""
    last_name: str = ""
    middle_name: str = ""
    title: str = ""
    suffix: str = ""
    full_name: str = ""
    company: str = ""
    job_title: str = ""
    department: str = ""
    office: str = ""
    emails: Tuple[str, ...] = ()
    phones: Tuple[Tuple[str, str], ...] = ()
    addresses: Tuple[Address, ...] = ()
    birthday: Optional[date] = None
    anniversary: Optional[date] = None
    spouse: str = ""
    children: str = ""
    assistant: str = ""
    web_page: str = ""
    notes: str = ""
    categories: Tuple[str, ...] = ()
    uid: str = ""

    @classmethod
    def _revive(cls, name: str, value: Any) -> Any:
        if name in ("birthday", "anniversary") and isinstance(value, str):
            moment = parse_iso(value)
            return moment.date() if isinstance(moment, datetime) else moment
        if name == "phones":
            return tuple((str(k), str(n)) for k, n in (value or ()))
        if name == "addresses":
            return tuple(Address.from_dict(a) if isinstance(a, dict) else a for a in (value or ()))
        if name in ("emails", "categories"):
            return tuple(value or ())
        return value

    def display_name(self) -> str:
        if _clean(self.full_name):
            return _clean(self.full_name)
        parts = [self.first_name, self.middle_name, self.last_name]
        joined = " ".join(p for p in (_clean(x) for x in parts) if p)
        return joined or _clean(self.company) or (self.emails[0] if self.emails else "")

    def normalized(self) -> "Contact":
        text_fields = {f.name: _clean(getattr(self, f.name)) for f in fields(self)
                       if f.type == "str" and f.name not in ("notes", "uid")}
        phones = tuple((k if k in PHONE_KINDS else "work", _clean(n)) for k, n in self.phones if _clean(n))
        addresses = tuple(a for a in self.addresses if not a.is_empty)
        return replace(self, **text_fields, emails=_clean_list(self.emails), phones=phones,
                       addresses=addresses, notes=self.notes.replace("\r\n", "\n").strip(),
                       categories=_clean_list(self.categories))

    def match_key(self) -> str:
        me = self.normalized()
        email = me.emails[0].casefold() if me.emails else ""
        return f"contact|{me.display_name().casefold()}|{email}"


@dataclass(frozen=True)
class Note(Record):
    title: str
    body: str = ""
    folder: str = ""
    modified: Optional[datetime] = None
    uid: str = ""

    HASH_EXCLUDE: ClassVar[Tuple[str, ...]] = ("uid", "folder")

    @classmethod
    def _revive(cls, name: str, value: Any) -> Any:
        if name == "modified" and isinstance(value, str):
            moment = parse_iso(value)
            return moment if isinstance(moment, datetime) else datetime(moment.year, moment.month, moment.day)
        return value

    def normalized(self) -> "Note":
        return replace(self, title=_clean(self.title), body=self.body.replace("\r\n", "\n").rstrip(),
                       modified=None)

    def match_key(self) -> str:
        return f"note|{self.normalized().title.casefold()}"


@dataclass(frozen=True)
class Document(Record):
    """A document or spreadsheet as text: ``kind`` is "text" (paragraphs) or "sheet" (CSV)."""

    name: str
    text: str = ""
    kind: str = "text"
    modified: Optional[datetime] = None
    uid: str = ""

    @classmethod
    def _revive(cls, name: str, value: Any) -> Any:
        if name == "modified" and isinstance(value, str):
            moment = parse_iso(value)
            return moment if isinstance(moment, datetime) else datetime(moment.year, moment.month, moment.day)
        return value

    def normalized(self) -> "Document":
        return replace(self, name=_clean(self.name), text=self.text.replace("\r\n", "\n").rstrip(),
                       modified=None)

    def match_key(self) -> str:
        return f"doc|{self.normalized().name.casefold()}"


__all__ = ["Record", "Appointment", "Task", "Contact", "Address", "Note", "Document",
           "BUSY_STATES", "PRIORITIES", "PHONE_KINDS", "ADDRESS_KINDS", "field", "asdict"]
