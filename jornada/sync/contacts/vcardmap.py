"""Contact ⇄ vCard, for the CardDAV backend (and any vCard a server hands over).

Property map (both directions unless noted)::

    N            family;given;additional;prefix;suffix   ← last, first, middle, title, suffix
    FN           display name (kept as full_name only when it differs from the assembled one)
    ORG          company;department;office
    TITLE        job_title
    TEL          TYPE per kind: work → WORK,VOICE (a second one is work2), home → HOME,VOICE
                 (second: home2), mobile → CELL, work_fax → WORK,FAX, home_fax → HOME,FAX,
                 pager → PAGER, car → CAR, radio → X-RADIO, assistant → X-ASSISTANT;
                 reading: FAX(+HOME) → home_fax / work_fax, CELL → mobile, PAGER, CAR, X-RADIO,
                 X-ASSISTANT, HOME (second: home2), anything else → work (second: work2)
    EMAIL        up to three; the first written PREF, a PREF one read first
    ADR          TYPE home / work / other; seven parts with po box and extended empty
                 (reading folds a po box or extended address into the street)
    BDAY, ANNIVERSARY (written together with X-ANNIVERSARY; X-EVOLUTION-ANNIVERSARY and
                 Apple's labelled X-ABDATE are read too)
    NOTE, CATEGORIES, URL (web_page), UID
    X-SPOUSE (or X-EVOLUTION-SPOUSE), X-CHILDREN, X-ASSISTANT (or X-EVOLUTION-ASSISTANT)

Anything else (PHOTO, NICKNAME, IMPP, X-ABLabel groups, …) is not modelled and does
not survive a rewrite: an update regenerates the whole card from the record.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from ...pim.models import Address, Contact
from ...webapi import vcard
from ...webapi.ical import Property
from ...webapi.vcard import VCard
from .common import (MAX_EMAILS, address, assign_addresses, assign_phone_kinds, join_names, limit_emails, slot_order,
                     split_names, text, with_full_name)

TEL_TYPES = {
    "work": ("WORK", "VOICE"), "work2": ("WORK", "VOICE"), "home": ("HOME", "VOICE"), "home2": ("HOME", "VOICE"),
    "mobile": ("CELL",), "work_fax": ("WORK", "FAX"), "home_fax": ("HOME", "FAX"), "pager": ("PAGER",),
    "car": ("CAR",), "radio": ("X-RADIO",), "assistant": ("X-ASSISTANT",),
}
_TYPE_KINDS = (("cell", "mobile"), ("pager", "pager"), ("car", "car"), ("x-radio", "radio"),
               ("x-assistant", "assistant"), ("home", "home"))
FIRST_EMAIL_TYPES = ("INTERNET", "PREF")
OTHER_EMAIL_TYPES = ("INTERNET",)
TEL_URI_PREFIX = "tel:"
SPOUSE_NAMES = ("X-SPOUSE", "X-EVOLUTION-SPOUSE")
ASSISTANT_NAMES = ("X-ASSISTANT", "X-EVOLUTION-ASSISTANT")
CHILDREN_NAME = "X-CHILDREN"
ANNIVERSARY_ALIAS = "X-ANNIVERSARY"
STREET_SEPARATOR = "\n"
ORG_PARTS = 3


# -- reading --------------------------------------------------------------------------
def phone_kind(types: Sequence[str]) -> str:
    """TEL TYPE values → the device's phone kind, before a second work/home number overflows."""
    lowered = {value.lower() for value in types}
    if "fax" in lowered:
        return "home_fax" if "home" in lowered else "work_fax"
    return next((kind for token, kind in _TYPE_KINDS if token in lowered), "work")


def phone_number(value: str) -> str:
    """The number of a TEL value; a vCard 4 ``tel:`` URI loses its scheme."""
    cleaned = text(value)
    return cleaned[len(TEL_URI_PREFIX):].strip() if cleaned.lower().startswith(TEL_URI_PREFIX) else cleaned


def address_kind(types: Sequence[str]) -> str:
    lowered = {value.lower() for value in types}
    return "home" if "home" in lowered else "work" if "work" in lowered else "other"


def _address_of(parts: Sequence[str], types: Sequence[str]) -> Optional[Address]:
    padded = (tuple(parts) + ("",) * vcard.ADR_PARTS)[:vcard.ADR_PARTS]
    po_box, extended, street, city, state, postal_code, country = padded
    lines = [line for line in (text(po_box), text(extended), text(street)) if line]
    return address(address_kind(types), STREET_SEPARATOR.join(lines), city, state, postal_code, country)


def _emails_of(card: VCard) -> Tuple[str, ...]:
    """Addresses in card order, a PREF one first (it becomes the device's primary address)."""
    entries = vcard.emails(card)
    preferred = [value for value, types in entries if "pref" in types]
    others = [value for value, types in entries if "pref" not in types]
    return limit_emails(preferred + others)


def _phones_of(card: VCard) -> Tuple[Tuple[str, str], ...]:
    pairs = ((phone_kind(types), phone_number(number)) for number, types in vcard.telephones(card))
    return slot_order(assign_phone_kinds(pairs))


def _org_of(card: VCard) -> Tuple[str, str, str]:
    parts = (vcard.org_parts(card) + ("",) * ORG_PARTS)[:ORG_PARTS]
    return parts[0], parts[1], parts[2]


def _first_value(card: VCard, names: Iterable[str]) -> str:
    return next((text(card.value(name)) for name in names if text(card.value(name))), "")


def _children_of(card: VCard) -> str:
    return join_names(name for prop in card.get_all(CHILDREN_NAME) for name in split_names(prop.value))


def from_vcard(card: VCard) -> Contact:
    """A parsed vCard → Contact; properties the device cannot hold are ignored."""
    family, given, additional, prefix, suffix = vcard.name_parts(card)
    company, department, office = _org_of(card)
    contact = Contact(
        first_name=given, last_name=family, middle_name=additional, title=prefix, suffix=suffix,
        company=company, job_title=text(card.value("TITLE")), department=department, office=office,
        emails=_emails_of(card),
        phones=_phones_of(card),
        addresses=assign_addresses(_address_of(parts, types) for parts, types in vcard.addresses(card)),
        birthday=vcard.birthday(card),
        anniversary=vcard.anniversary(card),
        spouse=_first_value(card, SPOUSE_NAMES),
        children=_children_of(card),
        assistant=_first_value(card, ASSISTANT_NAMES),
        web_page=text(card.value("URL")),
        notes=(card.value("NOTE") or "").replace("\r\n", "\n").strip(),
        categories=vcard.split_list(card.value("CATEGORIES") or ""),
        uid=text(card.value("UID")),
    )
    return with_full_name(contact, card.value("FN"))


# -- writing --------------------------------------------------------------------------
def _trim_trailing(parts: Sequence[str]) -> Tuple[str, ...]:
    kept = list(parts)
    while kept and not kept[-1]:
        kept.pop()
    return tuple(kept)


def _email_entries(emails: Sequence[str]) -> List[vcard.TypedValue]:
    return [(email, FIRST_EMAIL_TYPES if index == 0 else OTHER_EMAIL_TYPES)
            for index, email in enumerate(emails[:MAX_EMAILS])]


def _adr_entries(addresses: Iterable[Address]) -> List[vcard.Address]:
    return [(("", "", a.street, a.city, a.state, a.postal_code, a.country), (a.kind.upper(),))
            for a in assign_addresses(addresses)]


def _extra_properties(contact: Contact) -> List[Property]:
    anniversary = contact.anniversary.strftime("%Y%m%d") if contact.anniversary else ""
    pairs = ((ANNIVERSARY_ALIAS, anniversary), (SPOUSE_NAMES[0], contact.spouse), (CHILDREN_NAME, contact.children),
             (ASSISTANT_NAMES[0], contact.assistant))
    return [Property(name, value) for name, value in pairs if value]


def to_vcard(contact: Contact, uid: str) -> VCard:
    """Contact → a vCard carrying ``uid``; numbers are written in the device's slot order."""
    item = contact.normalized()
    return vcard.make_vcard(
        uid,
        family=item.last_name, given=item.first_name, additional=item.middle_name, prefix=item.title,
        suffix=item.suffix,
        fn=item.display_name() or None,
        org=_trim_trailing((item.company, item.department, item.office)),
        title=item.job_title or None,
        tels=[(number, TEL_TYPES[kind]) for kind, number in slot_order(item.phones)],
        emails=_email_entries(item.emails),
        adrs=_adr_entries(item.addresses),
        bday=item.birthday,
        anniversary=item.anniversary,
        note=item.notes or None,
        categories=item.categories,
        url=item.web_page or None,
        extra=_extra_properties(item),
    )


__all__ = ["from_vcard", "to_vcard", "phone_kind", "phone_number", "address_kind", "TEL_TYPES"]
