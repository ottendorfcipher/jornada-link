"""Helpers the contacts backends share: phone-slot assignment, the display-name rule,
ISO dates, bounded e-mail and address lists and the HTTP-error-to-StoreError translation.

The Jornada stores exactly one number per kind (work, work2, home, home2, mobile,
work_fax, home_fax, pager, car, radio, assistant), three e-mail addresses and one
address each for home, work and other. Every backend reads its service into
those slots the same way: :func:`assign_phone_kinds` gives a second work or home
number the ``work2`` / ``home2`` slot, :func:`assign_addresses` gives a second
address of a kind the free ``other`` slot, and :func:`slot_order` lists numbers
the way the device codec does, so a record looks the same whichever side made it.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ...pim.models import ADDRESS_KINDS, PHONE_KINDS, Address, Contact
from ...webapi.http import HttpClient, HttpError, HttpResponse
from ..base import StoreError

Log = Callable[[str], None]
MAX_EMAILS = 3
OVERFLOW_KINDS = {"work": "work2", "home": "home2"}
SPARE_ADDRESS_KIND = "other"
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_NAME_SEPARATORS = re.compile(r"[,;]")
_SLOT_INDEX = {kind: index for index, kind in enumerate(PHONE_KINDS)}

Phones = Tuple[Tuple[str, str], ...]


def text(value: Any) -> str:
    """``value`` as stripped text; ``None`` and non-strings become ``""``."""
    return value.strip() if isinstance(value, str) else ""


def strings_of(value: Any) -> Tuple[str, ...]:
    """The non-blank strings of a JSON list (anything else is ignored)."""
    if not isinstance(value, list):
        return ()
    return tuple(cleaned for cleaned in (text(entry) for entry in value) if cleaned)


# -- phones -----------------------------------------------------------------------
def assign_phone_kinds(pairs: Iterable[Tuple[str, str]]) -> Phones:
    """``(kind, number)`` pairs as a service reports them → the device's slots.

    Blank numbers are dropped, unknown kinds become ``work``, and a second work or
    home number takes the ``work2`` / ``home2`` slot. Further numbers of the same
    kind keep the base kind (the device stores only two of each).
    """
    assigned = []
    taken = set()
    for kind, number in pairs:
        cleaned = text(number)
        if not cleaned:
            continue
        slot = kind if kind in PHONE_KINDS else "work"
        overflow = OVERFLOW_KINDS.get(slot)
        if slot in taken and overflow and overflow not in taken:
            slot = overflow
        taken.add(slot)
        assigned.append((slot, cleaned))
    return tuple(assigned)


def slot_order(phones: Iterable[Tuple[str, str]]) -> Phones:
    """The numbers in the device's slot order (work, work2, home, home2, mobile, …).

    A stable sort, so two numbers of one kind keep their relative order and a
    second work number always follows the first — which is how the codec lists them.
    """
    return tuple(sorted(phones, key=lambda pair: _SLOT_INDEX.get(pair[0], len(PHONE_KINDS))))


def numbers_of(phones: Iterable[Tuple[str, str]], *kinds: str) -> Tuple[str, ...]:
    """The numbers of the given kinds, in the order the kinds are listed."""
    listed = tuple(phones)
    return tuple(number for wanted in kinds for kind, number in listed if kind == wanted)


# -- e-mail, names, dates ----------------------------------------------------------
def limit_emails(values: Iterable[Any]) -> Tuple[str, ...]:
    """The first three distinct, non-blank addresses."""
    kept: List[str] = []
    for value in values:
        cleaned = text(value)
        if cleaned and cleaned not in kept:
            kept.append(cleaned)
    return tuple(kept[:MAX_EMAILS])


def full_name_of(contact: Contact, formatted: Any = None) -> str:
    """The service's formatted name, else the name the record assembles itself.

    The device codec always fills ``full_name`` (Pocket Outlook stores the full
    name as its own field), so a record read from a service carries one too:
    identical contacts then have identical fingerprints on both sides, and a
    formatted name that really differs (``"Bob 'Hawk' Smith"``) reaches the Jornada.
    """
    return text(formatted) or contact.display_name()


def with_full_name(contact: Contact, formatted: Any = None) -> Contact:
    return replace(contact, full_name=full_name_of(contact, formatted))


def split_names(value: Any) -> Tuple[str, ...]:
    """``"Tom, Anna; Lee"`` → ``("Tom", "Anna", "Lee")``: the device keeps children as one line."""
    return tuple(part for part in (piece.strip() for piece in _NAME_SEPARATORS.split(text(value))) if part)


def join_names(values: Iterable[Any]) -> str:
    """The inverse of :func:`split_names`: one comma-separated line."""
    return ", ".join(part for part in (text(value) for value in values) if part)


def iso_date(value: Any) -> Optional[_dt.date]:
    """``YYYY-MM-DD`` (optionally followed by a time) → date; anything else → None."""
    match = _ISO_DATE.match(text(value))
    if not match:
        return None
    try:
        return _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


# -- addresses ------------------------------------------------------------------------
def address(kind: str, street: Any, city: Any, state: Any, postal_code: Any, country: Any) -> Optional[Address]:
    """An :class:`Address` of ``kind`` (unknown kinds become ``other``), or None when all parts are blank."""
    made = Address(kind if kind in ADDRESS_KINDS else SPARE_ADDRESS_KIND, text(street), text(city), text(state),
                   text(postal_code), text(country))
    return None if made.is_empty else made


def assign_addresses(addresses: Iterable[Optional[Address]]) -> Tuple[Address, ...]:
    """One address per kind: the first of each kind is kept, a second one of the same kind
    takes the free ``other`` slot, anything beyond that is dropped (the device has three slots)."""
    kept: List[Address] = []
    taken = set()
    for made in addresses:
        if made is None or made.is_empty:
            continue
        kind = made.kind if made.kind in ADDRESS_KINDS else SPARE_ADDRESS_KIND
        if kind in taken:
            if SPARE_ADDRESS_KIND in taken:
                continue
            kind = SPARE_ADDRESS_KIND
        taken.add(kind)
        kept.append(made if made.kind == kind else replace(made, kind=kind))
    return tuple(kept)


def address_of_kind(contact: Contact, kind: str) -> Optional[Address]:
    """The first address of ``kind`` in the record, or None."""
    return next((a for a in contact.addresses if a.kind == kind and not a.is_empty), None)


# -- HTTP -----------------------------------------------------------------------------
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


def item_id_of(payload: Dict[str, Any], service: str, what: str, key: str = "id") -> str:
    """The non-empty string under ``key`` of a response, or a StoreError."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise StoreError(f"{service} {what}: the response carries no {key}")
    return value


def string_of(payload: Dict[str, Any], key: str) -> str:
    return text(payload.get(key))


def objects_of(payload: Dict[str, Any], key: str) -> Tuple[Dict[str, Any], ...]:
    """The dict entries of the list under ``key`` (anything else is ignored)."""
    values = payload.get(key)
    if not isinstance(values, list):
        return ()
    return tuple(entry for entry in values if isinstance(entry, dict))


def first_object(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    """The first dict entry of the list under ``key``, or ``{}``."""
    found = objects_of(payload, key)
    return found[0] if found else {}


__all__ = ["Log", "MAX_EMAILS", "Phones", "text", "strings_of", "assign_phone_kinds", "slot_order", "numbers_of",
           "limit_emails", "full_name_of", "with_full_name", "split_names", "join_names", "iso_date", "address",
           "assign_addresses", "address_of_kind", "send", "json_object", "item_id_of", "string_of", "objects_of",
           "first_object"]
