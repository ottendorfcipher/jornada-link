"""Backend "caldav": one calendar collection on any CalDAV server (Fastmail, Nextcloud, iCloud,
Radicale, Baïkal, …) ⇄ the device's appointments.

Items are the calendar objects themselves: the id is the object's path on the
server, the version its ETag. Reads are one calendar-query REPORT for the sync
window; writes are conditional (``If-None-Match: *`` / ``If-Match``) so a change
made on the server between listing and writing is reported, never overwritten.
An update regenerates the object from the device record under the same UID.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, TypeVar
from urllib.parse import urlsplit

from ...pim.models import Appointment
from ...webapi import ical
from ...webapi.dav import (Collection, DavClient, DavConflict, DavError, DavItem, DavNotFound, calendar_query,
                           discover_calendars)
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import Log, local_aware
from .icalmap import from_vevent, master_vevent, to_vevent
from .window import resolve_window, windowed

SERVICE = "CalDAV"
URL_SETTING = "url"
USERNAME_SETTING = "username"
PASSWORD_SETTING = "password"
CALENDAR_SETTING = "calendar"
TRANSPORT_KEY = "dav_transport"   # BuildContext.extra hook: a dav.Transport for tests
CONTENT_TYPE = "text/calendar; charset=utf-8"
EVENT_COMPONENT = "VEVENT"
OBJECT_SUFFIX = ".ics"
T = TypeVar("T")
UidFactory = Callable[[], str]


@dataclass(frozen=True)
class KnownObject:
    """What the last listing (or fetch) told us about one calendar object."""

    etag: Optional[str]
    uid: str
    recurring: bool


class UnreadableObject(ValueError):
    """A calendar object that is not a plain event or series master, or cannot be parsed at all."""


def new_uid() -> str:
    return str(uuid.uuid4())


def read_object(data: str, etag: Optional[str]) -> Tuple[Appointment, KnownObject]:
    """The event of one calendar object and what to remember about it (raises :class:`UnreadableObject`)."""
    try:
        master = master_vevent(ical.parse(data))
        record = from_vevent(master) if master is not None else None
    except (ical.IcalError, OverflowError) as exc:
        raise UnreadableObject(str(exc)) from exc
    if record is None:
        raise UnreadableObject("it holds no event (or only recurrence overrides)")
    return record, KnownObject(etag, record.uid, record.recurring)


def object_path(href: str) -> str:
    """The server path of an object (hrefs may be absolute or relative): the store's item id."""
    return urlsplit(href).path or href


def choose_collection(collections: Sequence[Collection], wanted: str) -> Collection:
    """The collection called ``wanted`` (display name or href), else the first one holding events."""
    name = wanted.strip()
    if name:
        for collection in collections:
            if collection.display_name.casefold() == name.casefold() or _same_path(collection.href, name):
                return collection
        names = ", ".join(c.display_name for c in collections) or "none"
        raise StoreError(f"{SERVICE}: no calendar called {name!r} on the server (found: {names})")
    for collection in collections:
        if EVENT_COMPONENT in collection.components or not collection.components:
            return collection
    raise StoreError(f"{SERVICE}: the server has no calendar that holds events")


def _same_path(href: str, other: str) -> bool:
    return object_path(href).rstrip("/") == object_path(other).rstrip("/") != ""


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip() or any(c.isspace() for c in item_id):
        raise StoreError(f"{SERVICE}: {item_id!r} is not a calendar object path")


class CalDavCalendarStore:
    """Store protocol over the events of one CalDAV collection inside the window."""

    name = "caldav"

    def __init__(self, client: DavClient, start: datetime, end: datetime, calendar: str = "",
                 log: Log = lambda _line: None, uid_factory: UidFactory = new_uid) -> None:
        self._client = client
        self._start = start
        self._end = end
        self._calendar = calendar
        self._log = log
        self._new_uid = uid_factory
        self._collection: Optional[Collection] = None
        self._known: Dict[str, KnownObject] = {}

    # -- plumbing -----------------------------------------------------------------
    def _dav(self, what: str, action: Callable[[], T]) -> T:
        try:
            return action()
        except DavConflict as exc:
            raise StoreError(f"{SERVICE}: could not {what}: the event changed on the server since it was "
                             "listed; run the sync again") from exc
        except DavNotFound as exc:
            raise StoreError(f"{SERVICE}: could not {what}: the event is no longer on the server") from exc
        except DavError as exc:
            raise StoreError(f"{SERVICE}: could not {what}: {exc.message}") from exc

    def collection(self) -> Collection:
        """The calendar collection this store syncs (discovered once)."""
        if self._collection is None:
            found = self._dav("find the calendars", lambda: discover_calendars(self._client))
            self._collection = choose_collection(found, self._calendar)
        return self._collection

    def _fetch(self, path: str, what: str) -> KnownObject:
        etag, body = self._dav(what, lambda: self._client.get(path))
        try:
            return read_object(body.decode("utf-8", errors="replace"), etag)[1]
        except UnreadableObject as exc:
            raise StoreError(f"{SERVICE}: could not {what}: the object on the server is not a plain event ({exc})") from exc

    def _known_object(self, path: str, what: str) -> KnownObject:
        return self._known.get(path) or self._fetch(path, what)

    # -- Store protocol -----------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        href = self.collection().href
        found = self._dav("list the events", lambda: calendar_query(
            self._client, href, EVENT_COMPONENT, start=local_aware(self._start), end=local_aware(self._end)))
        return tuple(self._item(entry) for entry in found)

    def _item(self, entry: DavItem) -> Item:
        path = object_path(entry.href)
        try:
            record, known = read_object(entry.data or "", entry.etag)
        except UnreadableObject as exc:
            self._log(f"{path} cannot be read ({exc}); it is left alone")
            return Item(id=path, record=None, version=entry.etag, problem=str(exc))
        self._known[path] = known
        return Item(id=path, record=record, version=entry.etag, read_only=record.recurring)

    def create(self, record: Appointment) -> str:
        uid = self._new_uid()
        path = object_path(self.collection().href).rstrip("/") + "/" + uid + OBJECT_SUFFIX
        body = ical.serialize(ical.vcalendar(to_vevent(record, uid))).encode("utf-8")
        etag = self._dav(f"create the event {record.summary!r}",
                         lambda: self._client.put(path, body, CONTENT_TYPE, if_none_match="*"))
        self._known[path] = KnownObject(etag, uid, False)
        return path

    def update(self, item_id: str, record: Appointment) -> Optional[str]:
        _check_id(item_id)
        what = f"update the event {record.summary!r}"
        known = self._known_object(item_id, what)
        if known.recurring:
            raise StoreError(f"{record.summary!r} is a recurring series on the server; it is left unchanged "
                             "(change it there)")
        uid = known.uid or self._new_uid()
        body = ical.serialize(ical.vcalendar(to_vevent(record, uid))).encode("utf-8")
        etag = self._dav(what, lambda: self._client.put(item_id, body, CONTENT_TYPE, if_match=known.etag))
        self._known[item_id] = KnownObject(etag, uid, False)
        return etag

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        known = self._known.get(item_id)
        try:
            self._client.delete(item_id, if_match=known.etag if known else None)
        except DavNotFound:
            self._log(f"{item_id} was already gone from the server")
        except DavConflict as exc:
            raise StoreError(f"{SERVICE}: could not delete the event: it changed on the server since it was "
                             "listed; run the sync again") from exc
        except DavError as exc:
            raise StoreError(f"{SERVICE}: could not delete the event: {exc.message}") from exc
        self._known.pop(item_id, None)


# -- backend spec ---------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Any:
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
    start, end = resolve_window(account, context)
    store = CalDavCalendarStore(client, start, end, calendar=account.setting(CALENDAR_SETTING) or "",
                                log=context.log)
    return windowed(store, account, start, end)


BACKEND = BackendSpec(
    key="caldav",
    title="CalDAV (Fastmail, Nextcloud, iCloud, Radicale, …)",
    settings=(
        SettingSpec(URL_SETTING, "server or calendar URL (https://…; calendar discovery starts there)"),
        SettingSpec(USERNAME_SETTING, "user name"),
        SettingSpec(PASSWORD_SETTING, "password or app-specific password", secret=True),
        SettingSpec(CALENDAR_SETTING, "calendar display name or href (default: the first calendar that holds events)",
                    required=False),
    ),
    build=build,
    notes="Writes are conditional on ETags, so a change made on the server between listing and writing is "
          "reported instead of overwritten. Attendees, attachments and other properties this tool does not "
          "model are dropped when an event is rewritten from the device.",
)

__all__ = ["CalDavCalendarStore", "KnownObject", "UnreadableObject", "read_object", "BACKEND", "build",
           "choose_collection", "object_path", "new_uid",
           "SERVICE", "CONTENT_TYPE", "TRANSPORT_KEY", "URL_SETTING", "USERNAME_SETTING", "PASSWORD_SETTING",
           "CALENDAR_SETTING"]
