"""Contacts Database ⇄ :class:`~jornada.pim.models.Contact`."""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple

from ..cedb import CEVT_FILETIME, PropVal, Record
from . import ids
from .common import has_prop, notes_of, put_categories, put_notes, put_string, split_categories, string_of
from .models import Address, Contact
from .timeconv import date_to_filetime, filetime_to_date

_TEXT_FIELDS = {
    "first_name": ids.CONTACT_FIRST_NAME, "last_name": ids.CONTACT_LAST_NAME,
    "middle_name": ids.CONTACT_MIDDLE_NAME, "title": ids.CONTACT_TITLE, "suffix": ids.CONTACT_SUFFIX,
    "company": ids.CONTACT_COMPANY, "job_title": ids.CONTACT_JOB_TITLE,
    "department": ids.CONTACT_DEPARTMENT, "office": ids.CONTACT_OFFICE, "spouse": ids.CONTACT_SPOUSE,
    "children": ids.CONTACT_CHILDREN, "assistant": ids.CONTACT_ASSISTANT, "web_page": ids.CONTACT_WEB_PAGE,
}
_OVERFLOW = {"work": "work2", "home": "home2"}


def _date_of(record: Record, prop_id: int) -> Optional[date]:
    value = record.value(prop_id)
    return filetime_to_date(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _phones_of(record: Record) -> Tuple[Tuple[str, str], ...]:
    phones = []
    for kind, prop_id in ids.CONTACT_PHONE_SLOTS.items():
        number = string_of(record, prop_id)
        if number:
            phones.append((kind, number))
    return tuple(phones)


def _addresses_of(record: Record) -> Tuple[Address, ...]:
    found = []
    for kind, slots in ids.CONTACT_ADDRESS_SLOTS.items():
        parts = [string_of(record, prop_id) for prop_id in slots]
        address = Address(kind, *parts)
        if not address.is_empty:
            found.append(address)
    return tuple(found)


def decode(record: Record) -> Contact:
    text = {name: string_of(record, prop_id) for name, prop_id in _TEXT_FIELDS.items()}
    emails = tuple(e for e in (string_of(record, slot) for slot in ids.CONTACT_EMAIL_SLOTS) if e)
    return Contact(
        full_name=string_of(record, ids.CONTACT_FULL_NAME),
        emails=emails,
        phones=_phones_of(record),
        addresses=_addresses_of(record),
        birthday=_date_of(record, ids.CONTACT_BIRTHDAY),
        anniversary=_date_of(record, ids.CONTACT_ANNIVERSARY),
        notes=notes_of(record),
        categories=split_categories(record.value(ids.CONTACT_CATEGORIES, "")),
        **text,
    )


def _phone_slots(phones: Tuple[Tuple[str, str], ...]) -> Dict[str, str]:
    """Assign numbers to device slots; a second work/home number spills into work2/home2."""
    assigned: Dict[str, str] = {}
    for kind, number in phones:
        slot = kind if kind in ids.CONTACT_PHONE_SLOTS else "work"
        if slot in assigned and _OVERFLOW.get(slot) and _OVERFLOW[slot] not in assigned:
            slot = _OVERFLOW[slot]
        if slot not in assigned:
            assigned[slot] = number
    return assigned


def _put_date(props: List[PropVal], prop_id: int, day: Optional[date], existing: Optional[Record]) -> None:
    if day is not None:
        props.append(PropVal.filetime(prop_id, date_to_filetime(day)))
    elif has_prop(existing, prop_id):
        props.append(PropVal.deleted(prop_id, CEVT_FILETIME))


def encode(contact: Contact, existing: Optional[Record] = None) -> Tuple[PropVal, ...]:
    item = contact.normalized()
    props: List[PropVal] = []
    for name, prop_id in _TEXT_FIELDS.items():
        put_string(props, prop_id, getattr(item, name), existing)
    put_string(props, ids.CONTACT_FULL_NAME, item.display_name(), existing)
    for slot, email in zip(ids.CONTACT_EMAIL_SLOTS, list(item.emails[:3]) + ["", "", ""]):
        put_string(props, slot, email, existing)
    assigned = _phone_slots(item.phones)
    for kind, prop_id in ids.CONTACT_PHONE_SLOTS.items():
        put_string(props, prop_id, assigned.get(kind, ""), existing)
    by_kind = {a.kind: a for a in item.addresses}
    for kind, slots in ids.CONTACT_ADDRESS_SLOTS.items():
        address = by_kind.get(kind, Address(kind))
        for prop_id, value in zip(slots, (address.street, address.city, address.state,
                                          address.postal_code, address.country)):
            put_string(props, prop_id, value, existing)
    _put_date(props, ids.CONTACT_BIRTHDAY, item.birthday, existing)
    _put_date(props, ids.CONTACT_ANNIVERSARY, item.anniversary, existing)
    put_notes(props, ids.CONTACT_NOTE, item.notes, existing)
    put_categories(props, item.categories, existing)
    if not props:
        props.append(PropVal.string(ids.CONTACT_FULL_NAME, "(unnamed)"))
    return tuple(props)


def is_read_only(contact: Contact) -> bool:
    return False
