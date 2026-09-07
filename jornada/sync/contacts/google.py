"""Google Contacts backend: the People API ⇄ the device's contacts.

Contacts are the signed-in user's connections; with a ``group`` setting only the
members of that contact group are listed and a contact created from the device
joins it (the group is created when missing). Ids are People API resource
names (``people/c123``), versions are etags, and an update sends the etag it
listed so a contact edited in Google since then is reported, not overwritten.
Google has no categories, so the device's categories do not travel; a birthday
without a year reads as none.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ...pim.models import ADDRESS_KINDS, Address, Contact
from ...webapi.http import HttpClient
from ...webapi.oauth import GOOGLE
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import (MAX_EMAILS, Log, address, assign_addresses, assign_phone_kinds, first_object, item_id_of,
                     join_names, json_object, limit_emails, objects_of, send, slot_order, split_names, string_of,
                     text, with_full_name)

SERVICE = "Google Contacts"
PEOPLE_BASE = "https://people.googleapis.com/v1"
SCOPES = ("https://www.googleapis.com/auth/contacts",)
GROUP_SETTING = "group"
PAGE_SIZE = 200
MAX_PAGES = 400
PERSON_FIELDS = ("names,emailAddresses,phoneNumbers,addresses,organizations,birthdays,biographies,urls,"
                 "memberships,events,relations,metadata")
UPDATE_FIELDS = "names,emailAddresses,phoneNumbers,addresses,organizations,birthdays,biographies,urls,events,relations"
CONNECTIONS_PATH = "/people/me/connections"
GROUPS_PATH = "/contactGroups"
PEOPLE_PREFIX = "people/"
ANNIVERSARY_EVENT = "anniversary"
TEXT_PLAIN = "TEXT_PLAIN"
PHONE_KINDS_BY_TYPE = {"work": "work", "main": "work", "home": "home", "mobile": "mobile", "workmobile": "mobile",
                       "googlevoice": "mobile", "workfax": "work_fax", "homefax": "home_fax", "otherfax": "work_fax",
                       "pager": "pager", "workpager": "pager", "car": "car", "radio": "radio", "assistant": "assistant"}
PHONE_TYPES = {"work": "work", "work2": "work", "home": "home", "home2": "home", "mobile": "mobile",
               "work_fax": "workFax", "home_fax": "homeFax", "pager": "pager", "car": "car", "radio": "radio",
               "assistant": "assistant"}
RELATION_TYPES = {"spouse": "spouse", "child": "children", "assistant": "assistant"}


# -- mapping (pure) ---------------------------------------------------------------------
def date_of(value: Any) -> Optional[date]:
    """A People API ``Date`` (year, month, day) → date; None without a year or when invalid."""
    if not isinstance(value, dict):
        return None
    try:
        return date(int(value.get("year") or 0), int(value.get("month") or 0), int(value.get("day") or 0))
    except (TypeError, ValueError):
        return None


def date_payload(day: date) -> Dict[str, int]:
    return {"year": day.year, "month": day.month, "day": day.day}


def _is_primary(entry: Dict[str, Any]) -> bool:
    metadata = entry.get("metadata")
    return isinstance(metadata, dict) and metadata.get("primary") is True


def _emails_of(entry: Dict[str, Any]) -> Tuple[str, ...]:
    """Addresses with the primary one first (it becomes the device's first e-mail)."""
    listed = objects_of(entry, "emailAddresses")
    primary = [e.get("value") for e in listed if _is_primary(e)]
    others = [e.get("value") for e in listed if not _is_primary(e)]
    return limit_emails(primary + others)


def _phones_of(entry: Dict[str, Any]) -> Tuple[Tuple[str, str], ...]:
    pairs = ((PHONE_KINDS_BY_TYPE.get(string_of(p, "type").casefold(), "work"), p.get("value"))
             for p in objects_of(entry, "phoneNumbers"))
    return slot_order(assign_phone_kinds(pairs))


def _address_of(entry: Dict[str, Any]) -> Optional[Address]:
    lines = [line for line in (string_of(entry, key) for key in ("poBox", "extendedAddress", "streetAddress")) if line]
    kind = string_of(entry, "type").casefold()
    return address(kind if kind in ADDRESS_KINDS else "other", "\n".join(lines), entry.get("city"),
                   entry.get("region"), entry.get("postalCode"), entry.get("country") or entry.get("countryCode"))


def _anniversary_of(entry: Dict[str, Any]) -> Optional[date]:
    for event in objects_of(entry, "events"):
        if string_of(event, "type").casefold() == ANNIVERSARY_EVENT:
            found = date_of(event.get("date"))
            if found is not None:
                return found
    return None


def _relations_of(entry: Dict[str, Any]) -> Dict[str, str]:
    """``{"spouse": …, "children": "a, b", "assistant": …}`` from the relations list."""
    names: Dict[str, List[str]] = {field: [] for field in RELATION_TYPES.values()}
    for relation in objects_of(entry, "relations"):
        field = RELATION_TYPES.get(string_of(relation, "type").casefold())
        if field and string_of(relation, "person"):
            names[field].append(string_of(relation, "person"))
    return {field: join_names(found) for field, found in names.items()}


def person_of(entry: Dict[str, Any]) -> Contact:
    """A People API ``Person`` → Contact."""
    name = first_object(entry, "names")
    org = first_object(entry, "organizations")
    relations = _relations_of(entry)
    contact = Contact(
        first_name=string_of(name, "givenName"), last_name=string_of(name, "familyName"),
        middle_name=string_of(name, "middleName"), title=string_of(name, "honorificPrefix"),
        suffix=string_of(name, "honorificSuffix"),
        company=string_of(org, "name"), job_title=string_of(org, "title"), department=string_of(org, "department"),
        office=string_of(org, "location"),
        emails=_emails_of(entry),
        phones=_phones_of(entry),
        addresses=assign_addresses(_address_of(a) for a in objects_of(entry, "addresses")),
        birthday=date_of(first_object(entry, "birthdays").get("date")),
        anniversary=_anniversary_of(entry),
        spouse=relations["spouse"], children=relations["children"], assistant=relations["assistant"],
        web_page=string_of(first_object(entry, "urls"), "value"),
        notes=text(first_object(entry, "biographies").get("value")).replace("\r\n", "\n"),
        uid=string_of(entry, "resourceName"),
    )
    return with_full_name(contact)      # Google composes displayName itself; the record carries the assembled name


def _relations_payload(item: Contact) -> List[Dict[str, str]]:
    pairs = [(item.spouse, "spouse"), *((child, "child") for child in split_names(item.children)),
             (item.assistant, "assistant")]
    return [{"person": person, "type": kind} for person, kind in pairs if person]


def _addresses_payload(item: Contact) -> List[Dict[str, str]]:
    return [{"type": a.kind, "streetAddress": a.street, "city": a.city, "region": a.state,
             "postalCode": a.postal_code, "country": a.country} for a in assign_addresses(item.addresses)]


def payload_of(record: Contact) -> Dict[str, Any]:
    """Contact → the person fields of a create or update; an empty list clears a field."""
    item = record.normalized()
    name = {"givenName": item.first_name, "familyName": item.last_name, "middleName": item.middle_name,
            "honorificPrefix": item.title, "honorificSuffix": item.suffix}
    org = {"name": item.company, "title": item.job_title, "department": item.department, "location": item.office}
    return {
        "names": [name] if any(name.values()) else [],
        "organizations": [org] if any(org.values()) else [],
        "emailAddresses": [{"value": email} for email in item.emails[:MAX_EMAILS]],
        "phoneNumbers": [{"value": number, "type": PHONE_TYPES[kind]} for kind, number in slot_order(item.phones)],
        "addresses": _addresses_payload(item),
        "birthdays": [{"date": date_payload(item.birthday)}] if item.birthday else [],
        "biographies": [{"value": item.notes, "contentType": TEXT_PLAIN}] if item.notes else [],
        "urls": [{"value": item.web_page}] if item.web_page else [],
        "events": [{"type": ANNIVERSARY_EVENT, "date": date_payload(item.anniversary)}] if item.anniversary else [],
        "relations": _relations_payload(item),
    }


def is_member(entry: Dict[str, Any], group_resource: str) -> bool:
    for membership in objects_of(entry, "memberships"):
        group = membership.get("contactGroupMembership")
        if isinstance(group, dict) and group.get("contactGroupResourceName") == group_resource:
            return True
    return False


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.startswith(PEOPLE_PREFIX) or any(c.isspace() for c in item_id):
        raise StoreError(f"{SERVICE}: {item_id!r} is not a contact resource name")


# -- the store ---------------------------------------------------------------------------
class GoogleContactsStore:
    """Store protocol over the user's Google contacts (or the members of one contact group)."""

    name = "google-contacts"

    def __init__(self, http: HttpClient, group: str = "", log: Log = lambda _line: None) -> None:
        self._http = http
        self._group = group.strip()
        self._log = log
        self._group_resource: Optional[str] = None
        self._etags: Dict[str, str] = {}

    # -- API plumbing -----------------------------------------------------------
    def _json(self, method: str, path: str, what: str, **kwargs: Any) -> Dict[str, Any]:
        return json_object(send(self._http, SERVICE, method, path, **kwargs), SERVICE, what)

    def _pages(self, path: str, params: Dict[str, Any], key: str, what: str) -> List[Dict[str, Any]]:
        """Every entry under ``key`` of a paged listing (``nextPageToken``)."""
        entries: List[Dict[str, Any]] = []
        token: Optional[str] = None
        for _page in range(MAX_PAGES):
            payload = self._json("GET", path, what, params={**params, "pageToken": token})
            entries.extend(objects_of(payload, key))
            token = string_of(payload, "nextPageToken") or None
            if not token:
                return entries
        raise StoreError(f"{SERVICE}: the listing did not end after {MAX_PAGES} pages")

    def group_resource(self) -> Optional[str]:
        """The resource name of the configured contact group (found or created once); None without one."""
        if not self._group:
            return None
        if self._group_resource is None:
            self._group_resource = self._find_group() or self._create_group()
        return self._group_resource

    def _find_group(self) -> Optional[str]:
        wanted = self._group.casefold()
        for entry in self._pages(GROUPS_PATH, {"pageSize": PAGE_SIZE}, "contactGroups", "list the contact groups"):
            names = (string_of(entry, "name").casefold(), string_of(entry, "formattedName").casefold())
            if wanted in names or entry.get("resourceName") == self._group:
                return item_id_of(entry, SERVICE, "group lookup", key="resourceName")
        return None

    def _create_group(self) -> str:
        payload = self._json("POST", GROUPS_PATH, f"create the contact group {self._group!r}",
                             json_body={"contactGroup": {"name": self._group}})
        self._log(f"created {SERVICE} group {self._group}")
        return item_id_of(payload, SERVICE, "group creation", key="resourceName")

    def _fetch_etag(self, item_id: str) -> str:
        payload = self._json("GET", "/" + quote(item_id, safe="/"), "read the contact",
                             params={"personFields": "metadata"})
        return item_id_of(payload, SERVICE, "read", key="etag")

    # -- Store protocol -----------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        group = self.group_resource()
        params = {"personFields": PERSON_FIELDS, "pageSize": PAGE_SIZE}
        entries = self._pages(CONNECTIONS_PATH, params, "connections", "list the contacts")
        items = (self._item(entry, group) for entry in entries)
        return tuple(item for item in items if item is not None)

    def _item(self, entry: Dict[str, Any], group: Optional[str]) -> Optional[Item]:
        resource = string_of(entry, "resourceName")
        if not resource or (group is not None and not is_member(entry, group)):
            return None
        etag = string_of(entry, "etag")
        self._etags[resource] = etag
        return Item(id=resource, record=person_of(entry), version=etag or None)

    def create(self, record: Contact) -> str:
        body = payload_of(record)
        group = self.group_resource()
        if group:
            body = {**body, "memberships": [{"contactGroupMembership": {"contactGroupResourceName": group}}]}
        payload = self._json("POST", "/people:createContact", f"create the contact {record.display_name()!r}",
                             params={"personFields": PERSON_FIELDS}, json_body=body)
        resource = item_id_of(payload, SERVICE, "create", key="resourceName")
        self._etags[resource] = string_of(payload, "etag")
        return resource

    def update(self, item_id: str, record: Contact) -> Optional[str]:
        _check_id(item_id)
        etag = self._etags.get(item_id) or self._fetch_etag(item_id)
        payload = self._json("PATCH", f"/{quote(item_id, safe='/')}:updateContact",
                             f"update the contact {record.display_name()!r}",
                             params={"updatePersonFields": UPDATE_FIELDS},
                             json_body={"etag": etag, **payload_of(record)})
        self._etags[item_id] = string_of(payload, "etag")
        return string_of(payload, "etag") or None

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        response = send(self._http, SERVICE, "DELETE", f"/{quote(item_id, safe='/')}:deleteContact", accept_errors=True)
        if response.status == 404:
            self._log(f"{SERVICE} contact {item_id} was already gone")
        elif not response.ok:
            raise StoreError(f"{SERVICE} delete failed: HTTP {response.status} {response.text[:200].strip()}")
        self._etags.pop(item_id, None)


# -- backend spec ------------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> GoogleContactsStore:
    http = api_client(PEOPLE_BASE, GOOGLE, SCOPES, account, secrets, context)
    return GoogleContactsStore(http, account.setting(GROUP_SETTING) or "", log=context.log)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(GOOGLE, SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key="google",
    title="Google Contacts",
    settings=oauth_settings("Google") + (
        SettingSpec(GROUP_SETTING, "contact group (label) to sync; only its members take part and new contacts "
                                   "join it (default: every contact)", required=False),
    ),
    build=build,
    login=login,
    notes="Needs your own Google Cloud OAuth desktop client with the contacts scope. Google has no categories, "
          "so the device's categories do not travel; a birthday without a year is ignored.",
)

__all__ = ["GoogleContactsStore", "BACKEND", "build", "login", "person_of", "payload_of", "date_of", "date_payload",
           "is_member", "SERVICE", "PEOPLE_BASE", "SCOPES", "PERSON_FIELDS", "UPDATE_FIELDS", "CONNECTIONS_PATH",
           "GROUPS_PATH"]
