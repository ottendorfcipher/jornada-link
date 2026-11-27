"""Backend "carddav": one address book on any CardDAV server (Fastmail, Nextcloud, iCloud,
Radicale, Baïkal, …) ⇄ the device's contacts.

Items are the vCard resources themselves: the id is the card's path on the
server, the version its ETag. Reads are one addressbook-query REPORT; writes are
conditional (``If-None-Match: *`` / ``If-Match``) so a change made on the server
between listing and writing is reported, never overwritten. An update regenerates
the card from the device record under the same UID (see :mod:`vcardmap` for what
travels and what is dropped). Group cards (``KIND:group``) are not people and are
left out; a card that does not parse is listed as unreadable so the engine leaves
it and its device counterpart alone.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, TypeVar
from urllib.parse import urlsplit

from ...pim.models import Contact
from ...webapi import vcard
from ...webapi.dav import (Collection, DavClient, DavConflict, DavError, DavItem, DavNotFound, addressbook_query,
                           discover_addressbooks)
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import Log
from .vcardmap import from_vcard, to_vcard

SERVICE = "CardDAV"
URL_SETTING = "url"
USERNAME_SETTING = "username"
PASSWORD_SETTING = "password"
ADDRESSBOOK_SETTING = "addressbook"
TRANSPORT_KEY = "dav_transport"   # BuildContext.extra hook: a dav.Transport for tests
CONTENT_TYPE = "text/vcard; charset=utf-8"
OBJECT_SUFFIX = ".vcf"
GROUP_KIND = "group"
KIND_PROPERTIES = ("KIND", "X-ADDRESSBOOKSERVER-KIND")
T = TypeVar("T")
UidFactory = Callable[[], str]


@dataclass(frozen=True)
class KnownCard:
    """What the last listing (or fetch) told us about one card on the server."""

    etag: Optional[str]
    uid: str


class UnreadableCard(ValueError):
    """A resource that does not parse as a vCard, or holds none."""


def new_uid() -> str:
    return str(uuid.uuid4())


def read_card(data: str, etag: Optional[str]) -> Optional[Tuple[Contact, KnownCard]]:
    """The person of one vCard resource and what to remember about it; None for a group card.

    Raises :class:`UnreadableCard` when the data does not parse or holds no card.
    """
    try:
        cards = vcard.parse(data)
    except vcard.VCardError as exc:
        raise UnreadableCard(str(exc)) from exc
    if not cards:
        raise UnreadableCard("the resource holds no vCard")
    if is_group_card(cards[0]):
        return None
    record = from_vcard(cards[0])
    return record, KnownCard(etag, record.uid)


def object_path(href: str) -> str:
    """The server path of a resource (hrefs may be absolute or relative): the store's item id."""
    return urlsplit(href).path or href


def choose_addressbook(collections: Sequence[Collection], wanted: str) -> Collection:
    """The address book called ``wanted`` (display name or href), else the first one."""
    name = wanted.strip()
    if name:
        for collection in collections:
            if collection.display_name.casefold() == name.casefold() or _same_path(collection.href, name):
                return collection
        names = ", ".join(c.display_name for c in collections) or "none"
        raise StoreError(f"{SERVICE}: no address book called {name!r} on the server (found: {names})")
    if not collections:
        raise StoreError(f"{SERVICE}: the server has no address book")
    return collections[0]


def _same_path(href: str, other: str) -> bool:
    return object_path(href).rstrip("/") == object_path(other).rstrip("/") != ""


def is_group_card(card: vcard.VCard) -> bool:
    """A vCard that describes a group of contacts rather than a person."""
    return any((card.value(name) or "").strip().casefold() == GROUP_KIND for name in KIND_PROPERTIES)


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip() or any(c.isspace() for c in item_id):
        raise StoreError(f"{SERVICE}: {item_id!r} is not a card path")


class CardDavContactsStore:
    """Store protocol over the vCards of one CardDAV address book."""

    name = "carddav"

    def __init__(self, client: DavClient, addressbook: str = "", log: Log = lambda _line: None,
                 uid_factory: UidFactory = new_uid) -> None:
        self._client = client
        self._addressbook = addressbook
        self._log = log
        self._new_uid = uid_factory
        self._collection: Optional[Collection] = None
        self._known: Dict[str, KnownCard] = {}

    # -- plumbing -----------------------------------------------------------------
    def _dav(self, what: str, action: Callable[[], T]) -> T:
        try:
            return action()
        except DavConflict as exc:
            raise StoreError(f"{SERVICE}: could not {what}: the contact changed on the server since it was "
                             "listed; run the sync again") from exc
        except DavNotFound as exc:
            raise StoreError(f"{SERVICE}: could not {what}: the contact is no longer on the server") from exc
        except DavError as exc:
            raise StoreError(f"{SERVICE}: could not {what}: {exc.message}") from exc

    def collection(self) -> Collection:
        """The address book this store syncs (discovered once)."""
        if self._collection is None:
            found = self._dav("find the address books", lambda: discover_addressbooks(self._client))
            self._collection = choose_addressbook(found, self._addressbook)
        return self._collection

    def _fetch(self, path: str, what: str) -> KnownCard:
        etag, body = self._dav(what, lambda: self._client.get(path))
        try:
            parsed = read_card(body.decode("utf-8", errors="replace"), etag)
        except UnreadableCard as exc:
            raise StoreError(f"{SERVICE}: could not {what}: the card on the server cannot be read ({exc})") from exc
        if parsed is None:
            raise StoreError(f"{SERVICE}: could not {what}: the card on the server is a group, not a person")
        return parsed[1]

    def _known_card(self, path: str, what: str) -> KnownCard:
        return self._known.get(path) or self._fetch(path, what)

    def _body(self, record: Contact, uid: str) -> bytes:
        return vcard.serialize(to_vcard(record, uid)).encode("utf-8")

    # -- Store protocol -----------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        href = self.collection().href
        found = self._dav("list the contacts", lambda: addressbook_query(self._client, href))
        items = (self._item(entry) for entry in found)
        return tuple(item for item in items if item is not None)

    def _item(self, entry: DavItem) -> Optional[Item]:
        path = object_path(entry.href)
        try:
            parsed = read_card(entry.data or "", entry.etag)
        except UnreadableCard as exc:
            self._log(f"{path} cannot be read ({exc}); it is left alone")
            return Item(id=path, record=None, version=entry.etag, problem=str(exc))
        if parsed is None:
            self._log(f"skipping {path}: it is a group, not a person")
            return None
        record, known = parsed
        self._known[path] = known
        return Item(id=path, record=record, version=entry.etag)

    def create(self, record: Contact) -> str:
        uid = self._new_uid()
        path = object_path(self.collection().href).rstrip("/") + "/" + uid + OBJECT_SUFFIX
        what = f"create the contact {record.display_name()!r}"
        etag = self._dav(what, lambda: self._client.put(path, self._body(record, uid), CONTENT_TYPE, if_none_match="*"))
        self._known[path] = KnownCard(etag, uid)
        return path

    def update(self, item_id: str, record: Contact) -> Optional[str]:
        _check_id(item_id)
        what = f"update the contact {record.display_name()!r}"
        known = self._known_card(item_id, what)
        uid = known.uid or self._new_uid()
        etag = self._dav(what, lambda: self._client.put(item_id, self._body(record, uid), CONTENT_TYPE,
                                                        if_match=known.etag))
        self._known[item_id] = KnownCard(etag, uid)
        return etag

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        known = self._known.get(item_id)
        try:
            self._client.delete(item_id, if_match=known.etag if known else None)
        except DavNotFound:
            self._log(f"{item_id} was already gone from the server")
        except DavConflict as exc:
            raise StoreError(f"{SERVICE}: could not delete the contact: it changed on the server since it was "
                             "listed; run the sync again") from exc
        except DavError as exc:
            raise StoreError(f"{SERVICE}: could not delete the contact: {exc.message}") from exc
        self._known.pop(item_id, None)


# -- backend spec ---------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> CardDavContactsStore:
    url = (account.setting(URL_SETTING) or "").strip()
    username = (account.setting(USERNAME_SETTING) or "").strip()
    password = secrets.get(PASSWORD_SETTING)
    if not url or not username or not isinstance(password, str) or not password:
        raise AccountError(f"account {account.name!r} needs {URL_SETTING}, {USERNAME_SETTING} and "
                           f"{PASSWORD_SETTING} (use --ask {PASSWORD_SETTING} for the password)")
    try:
        client = DavClient(url, username, password, transport=context.extra.get(TRANSPORT_KEY))
    except DavError as exc:
        raise AccountError(f"account {account.name!r}: {exc.message}") from exc
    return CardDavContactsStore(client, addressbook=account.setting(ADDRESSBOOK_SETTING) or "", log=context.log)


BACKEND = BackendSpec(
    key="carddav",
    title="CardDAV (Fastmail, Nextcloud, iCloud, …)",
    settings=(
        SettingSpec(URL_SETTING, "server or address book URL (https://…; discovery starts there)"),
        SettingSpec(USERNAME_SETTING, "user name"),
        SettingSpec(PASSWORD_SETTING, "password or app-specific password", secret=True),
        SettingSpec(ADDRESSBOOK_SETTING, "address book display name or href (default: the first one)", required=False),
    ),
    build=build,
    notes="Writes are conditional on ETags, so a change made on the server between listing and writing is "
          "reported instead of overwritten. Photos, nicknames, instant-messaging handles and other properties "
          "this tool does not model are dropped when a card is rewritten from the device.",
)

__all__ = ["CardDavContactsStore", "KnownCard", "UnreadableCard", "BACKEND", "build", "read_card", "choose_addressbook",
           "object_path", "new_uid", "is_group_card", "SERVICE", "CONTENT_TYPE", "TRANSPORT_KEY", "URL_SETTING",
           "USERNAME_SETTING", "PASSWORD_SETTING", "ADDRESSBOOK_SETTING"]
