"""CalDAV backend: the VTODOs of one calendar collection ⇄ the device's tasks.

Works with any server that accepts VTODO objects (Nextcloud Tasks, Tasks.org,
Radicale, Baïkal, ...). Items are identified by their href path and versioned
by ETag: updates and deletes send ``If-Match`` so a concurrent change on the
server surfaces as an error instead of being overwritten.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from ...pim.models import Task
from ...webapi import dav, ical
from ...webapi.dav import Collection, DavClient, DavConflict, DavError, DavNotFound
from ...webapi.ical import Component, IcalError
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import Log
from .filters import wrap_completed
from .icalmap import calendar_with, first_vtodo, from_vtodo, to_vtodo

SERVICE = "CalDAV"
CONTENT_TYPE = "text/calendar; charset=utf-8"
URL_SETTING, USERNAME_SETTING, PASSWORD_SETTING, CALENDAR_SETTING = "url", "username", "password", "calendar"
DAV_TRANSPORT_KEY = "dav_transport"  # BuildContext.extra hook for tests


@dataclass(frozen=True)
class _Known:
    """What the store remembers about one object: its ETag, the parsed calendar and the VTODO's UID."""

    etag: Optional[str]
    calendar: Component
    uid: str


def path_of(href: str) -> str:
    """The path form of an href (the id of an item), absolute URLs stripped to their path."""
    return urlsplit(href).path or href


def choose_collection(collections: Tuple[Collection, ...], wanted: Optional[str]) -> Collection:
    """The calendar named/located ``wanted``, else the first one that accepts VTODO."""
    if wanted and wanted.strip():
        key = wanted.strip()
        for collection in collections:
            if collection.display_name.casefold() == key.casefold() or _same_href(collection.href, key):
                return collection
        known = ", ".join(c.display_name for c in collections) or "none"
        raise StoreError(f"{SERVICE}: no calendar called {wanted!r} (known: {known})")
    for collection in collections:
        if "VTODO" in collection.components:
            return collection
    for collection in collections:
        if not collection.components:  # the server did not say what it supports
            return collection
    raise StoreError(f"{SERVICE}: none of the calendars accepts tasks (VTODO)")


def _same_href(href: str, other: str) -> bool:
    return href.rstrip("/") == other.rstrip("/") or path_of(href).rstrip("/") == path_of(other).rstrip("/")


class CalDavTasksStore:
    """Store protocol over the VTODOs of one CalDAV collection."""

    name = "caldav"

    def __init__(self, client: DavClient, calendar: Optional[str] = None, log: Log = lambda _line: None,
                 zone: Optional[tzinfo] = None, new_uid: Callable[[], str] = lambda: str(uuid.uuid4())) -> None:
        self._client = client
        self._calendar = calendar
        self._log = log
        self._zone = zone
        self._new_uid = new_uid
        self._collection: Optional[str] = None
        self._known: Dict[str, _Known] = {}

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        try:
            objects = dav.calendar_query(self._client, self.collection_href(), component="VTODO")
        except DavError as exc:
            raise StoreError(f"{SERVICE} listing failed: {exc}") from exc
        items = []
        for obj in objects:
            item = self._item(obj)
            if item is not None:
                items.append(item)
        return tuple(items)

    def _item(self, obj: dav.DavItem) -> Optional[Item]:
        try:
            calendar = ical.parse(obj.data or "")
            todo = first_vtodo(calendar)
            if todo is None:
                raise IcalError("no VTODO component")
            record = from_vtodo(todo, self._zone)
        except IcalError as exc:
            self._log(f"skipping {obj.href}: {exc}")
            return None
        item_id = path_of(obj.href)
        self._known[item_id] = _Known(obj.etag, calendar, record.uid or self._new_uid())
        return Item(id=item_id, record=record, version=obj.etag)

    def create(self, record: Task) -> str:
        uid = self._new_uid()
        calendar = calendar_with(to_vtodo(record, uid, zone=self._zone))
        target = urljoin(self.collection_href(), f"{uid}.ics")
        etag = self._put(target, calendar, if_none_match="*", what=f"create {record.summary!r}")
        item_id = path_of(target)
        self._known[item_id] = _Known(etag, calendar, uid)
        return item_id

    def update(self, item_id: str, record: Task) -> Optional[str]:
        known = self._known.get(item_id) or self._fetch(item_id)
        todo = to_vtodo(record, known.uid, base=first_vtodo(known.calendar), zone=self._zone)
        calendar = calendar_with(todo, known.calendar)
        etag = self._put(item_id, calendar, if_match=known.etag, what=f"update {record.summary!r}")
        self._known[item_id] = _Known(etag, calendar, known.uid)
        return etag

    def delete(self, item_id: str) -> None:
        known = self._known.pop(item_id, None)
        try:
            self._client.delete(item_id, if_match=known.etag if known else None)
        except DavNotFound:
            self._log(f"{item_id} was already gone from the server")
        except DavConflict as exc:
            raise StoreError(f"{SERVICE}: {item_id} changed on the server since it was read; run the sync again") from exc
        except DavError as exc:
            raise StoreError(f"{SERVICE} delete failed: {exc}") from exc

    # -- server calls ---------------------------------------------------------
    def collection_href(self) -> str:
        if self._collection is None:
            try:
                collections = dav.discover_calendars(self._client)
            except DavError as exc:
                raise StoreError(f"{SERVICE} discovery failed: {exc}") from exc
            chosen = choose_collection(collections, self._calendar)
            self._log(f"{SERVICE}: syncing tasks with {chosen.display_name!r}")
            self._collection = chosen.href
        return self._collection

    def _put(self, target: str, calendar: Component, what: str, if_match: Optional[str] = None,
             if_none_match: Optional[str] = None) -> Optional[str]:
        body = ical.serialize(calendar).encode("utf-8")
        try:
            return self._client.put(target, body, CONTENT_TYPE, if_match=if_match, if_none_match=if_none_match)
        except DavConflict as exc:
            raise StoreError(f"{SERVICE} could not {what}: it changed on the server since it was read; "
                             "run the sync again") from exc
        except DavError as exc:
            raise StoreError(f"{SERVICE} could not {what}: {exc}") from exc

    def _fetch(self, item_id: str) -> _Known:
        try:
            etag, body = self._client.get(item_id)
            calendar = ical.parse(body.decode("utf-8", errors="replace"))
        except DavNotFound as exc:
            raise StoreError(f"{SERVICE}: {item_id} no longer exists on the server") from exc
        except (DavError, IcalError) as exc:
            raise StoreError(f"{SERVICE} could not read {item_id}: {exc}") from exc
        todo = first_vtodo(calendar)
        if todo is None:
            raise StoreError(f"{SERVICE}: {item_id} holds no VTODO")
        known = _Known(etag, calendar, todo.value("UID") or self._new_uid())
        self._known[item_id] = known
        return known


# -- backend spec ---------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Any:
    url = (account.setting(URL_SETTING) or "").strip()
    try:
        client = DavClient(url, account.setting(USERNAME_SETTING), str(secrets.get(PASSWORD_SETTING) or ""),
                           transport=context.extra.get(DAV_TRANSPORT_KEY))
    except DavError as exc:
        raise StoreError(f"{SERVICE}: {exc}") from exc
    store = CalDavTasksStore(client, account.setting(CALENDAR_SETTING), log=context.log)
    return wrap_completed(store, account, context.log)


BACKEND = BackendSpec(
    key="caldav",
    title="CalDAV tasks (VTODO: Nextcloud Tasks, Tasks.org, Radicale…)",
    settings=(
        SettingSpec(URL_SETTING, "server or calendar URL (https://cloud.example.com/remote.php/dav/ …)"),
        SettingSpec(USERNAME_SETTING, "login name"),
        SettingSpec(PASSWORD_SETTING, "password or app password", secret=True),
        SettingSpec(CALENDAR_SETTING, "calendar to sync, by display name or href (default: the first one that "
                                      "accepts tasks)", required=False),
    ),
    build=build,
    notes="Tasks are exchanged as VTODO objects; recurrence rules, alarms and other properties of an existing "
          "object are preserved on update.",
)

__all__ = ["CalDavTasksStore", "BACKEND", "build", "choose_collection", "path_of", "DAV_TRANSPORT_KEY"]
