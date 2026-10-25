"""Backend "m365": the contacts of a Microsoft 365 / Exchange Online mailbox through Microsoft Graph.

The default is the mailbox's own contacts (``/me/contacts``); a ``folder``
setting names a contact folder instead. Ids are Graph contact ids, versions
are ``changeKey``s. Graph knows two business and two home numbers, a mobile
number and no fax or pager fields, so a work fax rides in a free business
phone slot (a home fax in a free home slot) and pager, car, radio and
assistant numbers do not travel; a birthday is exchanged as the date part of
Graph's midnight stamp.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ...pim.models import Address, Contact
from ...webapi.http import HttpClient
from ...webapi.oauth import Provider, microsoft
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import (MAX_EMAILS, Log, address, address_of_kind, assign_addresses, assign_phone_kinds, item_id_of,
                     iso_date, join_names, json_object, limit_emails, numbers_of, objects_of, send, slot_order,
                     split_names, string_of, strings_of, text, with_full_name)

SERVICE = "Microsoft 365"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ("Contacts.ReadWrite", "offline_access")
TENANT_SETTING = "tenant"
FOLDER_SETTING = "folder"
DEFAULT_TENANT = "common"
DEFAULT_CONTACTS_PATH = "/me/contacts"
FOLDERS_PATH = "/me/contactFolders"
CONTACT_PATH = "/me/contacts"
PAGE_SIZE = 100
MAX_PAGES = 400
BUSINESS_PHONE_SLOTS = 2
HOME_PHONE_SLOTS = 2
SELECT_FIELDS = ("id,changeKey,givenName,middleName,surname,title,generation,displayName,companyName,jobTitle,"
                 "department,officeLocation,emailAddresses,businessPhones,homePhones,mobilePhone,businessAddress,"
                 "homeAddress,otherAddress,birthday,personalNotes,businessHomePage,spouseName,children,"
                 "assistantName,categories")
ADDRESS_FIELDS = {"work": "businessAddress", "home": "homeAddress", "other": "otherAddress"}
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")


# -- mapping (pure) -----------------------------------------------------------------
def _address_of(kind: str, value: Any) -> Optional[Address]:
    if not isinstance(value, dict):
        return None
    return address(kind, value.get("street"), value.get("city"), value.get("state"), value.get("postalCode"),
                   value.get("countryOrRegion"))


def _phone_pairs(entry: Dict[str, Any]) -> List[Tuple[str, str]]:
    return ([("work", number) for number in strings_of(entry.get("businessPhones"))]
            + [("home", number) for number in strings_of(entry.get("homePhones"))]
            + [("mobile", string_of(entry, "mobilePhone"))])


def contact_of(entry: Dict[str, Any]) -> Contact:
    """A Graph ``contact`` resource → Contact."""
    contact = Contact(
        first_name=string_of(entry, "givenName"), last_name=string_of(entry, "surname"),
        middle_name=string_of(entry, "middleName"), title=string_of(entry, "title"),
        suffix=string_of(entry, "generation"),
        company=string_of(entry, "companyName"), job_title=string_of(entry, "jobTitle"),
        department=string_of(entry, "department"), office=string_of(entry, "officeLocation"),
        emails=limit_emails(e.get("address") for e in objects_of(entry, "emailAddresses")),
        phones=slot_order(assign_phone_kinds(_phone_pairs(entry))),
        addresses=assign_addresses(_address_of(kind, entry.get(field)) for kind, field in ADDRESS_FIELDS.items()),
        birthday=iso_date(entry.get("birthday")),
        spouse=string_of(entry, "spouseName"),
        children=join_names(strings_of(entry.get("children"))),
        assistant=string_of(entry, "assistantName"),
        web_page=string_of(entry, "businessHomePage"),
        notes=text(entry.get("personalNotes")).replace("\r\n", "\n"),
        categories=strings_of(entry.get("categories")),
        uid=string_of(entry, "id"),
    )
    return with_full_name(contact, entry.get("displayName"))


def phone_slots(phones: Tuple[Tuple[str, str], ...], kinds: Tuple[str, ...], fax_kind: str, limit: int) -> List[str]:
    """The numbers of ``kinds`` and, in a slot left free, the fax of the same side (Graph has no fax field)."""
    return list(numbers_of(phones, *kinds) + numbers_of(phones, fax_kind))[:limit]


def _address_payload(found: Optional[Address]) -> Optional[Dict[str, str]]:
    if found is None:
        return None
    return {"street": found.street, "city": found.city, "state": found.state, "postalCode": found.postal_code,
            "countryOrRegion": found.country}


def payload_of(record: Contact) -> Dict[str, Any]:
    """Contact → the JSON body of a create (POST) or update (PATCH); ``null`` clears a field."""
    item = record.normalized()
    mobiles = numbers_of(item.phones, "mobile")
    body: Dict[str, Any] = {
        "givenName": item.first_name, "middleName": item.middle_name, "surname": item.last_name,
        "title": item.title, "generation": item.suffix,
        "companyName": item.company, "jobTitle": item.job_title, "department": item.department,
        "officeLocation": item.office,
        "emailAddresses": [{"address": email} for email in item.emails[:MAX_EMAILS]],
        "businessPhones": phone_slots(item.phones, ("work", "work2"), "work_fax", BUSINESS_PHONE_SLOTS),
        "homePhones": phone_slots(item.phones, ("home", "home2"), "home_fax", HOME_PHONE_SLOTS),
        "mobilePhone": mobiles[0] if mobiles else None,
        "businessAddress": _address_payload(address_of_kind(item, "work")),
        "homeAddress": _address_payload(address_of_kind(item, "home")),
        "otherAddress": _address_payload(address_of_kind(item, "other")),
        "birthday": f"{item.birthday.isoformat()}T00:00:00Z" if item.birthday else None,
        "personalNotes": item.notes, "businessHomePage": item.web_page, "spouseName": item.spouse,
        "children": list(split_names(item.children)), "assistantName": item.assistant,
        "categories": list(item.categories),
    }
    display = item.display_name()
    return {**body, "displayName": display} if display else body


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip() or any(c.isspace() for c in item_id):
        raise StoreError(f"{SERVICE} needs a contact id to update or delete")


# -- the store ------------------------------------------------------------------------
class GraphContactsStore:
    """Store protocol over the contacts of one Outlook contact folder (default: the mailbox's own)."""

    name = "m365-contacts"

    def __init__(self, http: HttpClient, folder: str = "", log: Log = lambda _line: None) -> None:
        self._http = http
        self._folder = folder.strip()
        self._log = log
        self._contacts_path: Optional[str] = None

    def contacts_path(self) -> str:
        """``/me/contacts`` or ``/me/contactFolders/{id}/contacts`` for the folder called ``folder``."""
        if self._contacts_path is None:
            self._contacts_path = DEFAULT_CONTACTS_PATH if not self._folder else self._find_folder()
        return self._contacts_path

    def _find_folder(self) -> str:
        wanted = self._folder.casefold()
        names: List[str] = []
        params = {"$select": "id,displayName", "$top": PAGE_SIZE}
        for entry in self._pages(FOLDERS_PATH, params, "list the contact folders"):
            name = string_of(entry, "displayName")
            names.append(name)
            if name.casefold() == wanted and string_of(entry, "id"):
                return f"{FOLDERS_PATH}/{quote(entry['id'], safe='')}/contacts"
        raise StoreError(f"{SERVICE}: no contact folder called {self._folder!r} (found: {', '.join(names) or 'none'})")

    def _json(self, method: str, url: str, what: str, **kwargs: Any) -> Dict[str, Any]:
        return json_object(send(self._http, SERVICE, method, url, **kwargs), SERVICE, what)

    def _pages(self, path: str, params: Optional[Dict[str, Any]], what: str) -> List[Dict[str, Any]]:
        """Every ``value`` entry of a paged listing, following ``@odata.nextLink``."""
        entries: List[Dict[str, Any]] = []
        url: Optional[str] = path
        for _page in range(MAX_PAGES):
            if not url:
                return entries
            payload = self._json("GET", url, what, params=params)
            entries.extend(objects_of(payload, "value"))
            url, params = string_of(payload, "@odata.nextLink") or None, None
        raise StoreError(f"{SERVICE}: the listing did not end after {MAX_PAGES} pages")

    # -- Store protocol -----------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        params = {"$top": PAGE_SIZE, "$select": SELECT_FIELDS}
        entries = self._pages(self.contacts_path(), params, "list the contacts")
        return tuple(Item(id=entry["id"], record=contact_of(entry), version=string_of(entry, "changeKey") or None)
                     for entry in entries if string_of(entry, "id"))

    def create(self, record: Contact) -> str:
        payload = self._json("POST", self.contacts_path(), f"create the contact {record.display_name()!r}",
                             json_body=payload_of(record))
        return item_id_of(payload, SERVICE, "create")

    def update(self, item_id: str, record: Contact) -> Optional[str]:
        _check_id(item_id)
        payload = self._json("PATCH", f"{CONTACT_PATH}/{quote(item_id, safe='')}",
                             f"update the contact {record.display_name()!r}", json_body=payload_of(record))
        return string_of(payload, "changeKey") or None

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        response = send(self._http, SERVICE, "DELETE", f"{CONTACT_PATH}/{quote(item_id, safe='')}", accept_errors=True)
        if response.status == 404:
            self._log(f"{SERVICE} contact {item_id} was already gone")
        elif not response.ok:
            raise StoreError(f"{SERVICE} delete failed: HTTP {response.status} {response.text[:200].strip()}")


# -- backend spec ---------------------------------------------------------------------
def tenant_of(account: Account) -> str:
    tenant = (account.setting(TENANT_SETTING) or "").strip() or DEFAULT_TENANT
    if not _TENANT.match(tenant):
        raise AccountError(f"account {account.name!r}: {TENANT_SETTING} must be a tenant id or domain, not {tenant!r}")
    return tenant


def provider_for(account: Account) -> Provider:
    return microsoft(tenant_of(account))


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> GraphContactsStore:
    http = api_client(GRAPH_BASE, provider_for(account), SCOPES, account, secrets, context)
    return GraphContactsStore(http, account.setting(FOLDER_SETTING) or "", log=context.log)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(provider_for(account), SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key="m365",
    title="Microsoft 365 contacts",
    settings=oauth_settings("Microsoft") + (
        SettingSpec(TENANT_SETTING, f"Azure AD tenant id or domain (default {DEFAULT_TENANT})", required=False,
                    default=DEFAULT_TENANT),
        SettingSpec(FOLDER_SETTING, "contact folder display name (default: the mailbox's own contacts)",
                    required=False),
    ),
    build=build,
    login=login,
    notes="Needs an Azure app registration (public client, redirect http://localhost) with Contacts.ReadWrite. "
          "Outlook has no fax or pager fields: a work fax takes a free business phone slot (a home fax a free "
          "home slot) and reads back as a second number; pager, car, radio and assistant numbers do not travel.",
)

__all__ = ["GraphContactsStore", "BACKEND", "build", "login", "contact_of", "payload_of", "phone_slots", "tenant_of",
           "provider_for", "SERVICE", "GRAPH_BASE", "SCOPES", "SELECT_FIELDS", "DEFAULT_CONTACTS_PATH", "FOLDERS_PATH"]
