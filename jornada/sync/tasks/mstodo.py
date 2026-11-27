"""Microsoft To Do backend: one To Do list ⇄ the device's tasks, through Microsoft Graph.

Graph returns ``dateTimeTimeZone`` values in UTC by default and the To Do apps
store a due date as the instant of local midnight, so a date is read by
converting that instant to the Mac's zone and written the same way (see
:func:`graph_datetime`); writing a bare ``T00:00:00 UTC`` would show the
previous day to anyone west of Greenwich.
"""
from __future__ import annotations

from datetime import date, datetime, tzinfo
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ...pim.models import Task
from ...pim.timeconv import to_wall_clock
from ...webapi.http import HttpClient
from ...webapi.ical import resolve_zone
from ...webapi.oauth import microsoft
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import (NO_SUBJECT, Log, completion_date, html_to_text, is_real_completion, item_id_of, json_object,
                     local_midnight_utc, parse_stamp, send, strings_of, text_of)
from .filters import wrap_completed

SERVICE = "Microsoft To Do"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ("Tasks.ReadWrite", "offline_access")
DEFAULT_TENANT = "common"
TENANT_SETTING = "tenant"
LIST_SETTING = "list"
DEFAULT_LIST_KIND = "defaultList"
PAGE_SIZE = 100
IMPORTANCE = ("low", "normal", "high")


# -- value mapping ------------------------------------------------------------------
def graph_date(value: Any, zone: Optional[tzinfo] = None) -> Optional[date]:
    """A ``dateTimeTimeZone`` object → the calendar date in ``zone`` (default: local)."""
    if not isinstance(value, dict):
        return None
    moment = parse_stamp(value.get("dateTime"))
    if moment is None:
        return None
    if not isinstance(moment, datetime):
        return moment
    if moment.tzinfo is None:
        source = resolve_zone(str(value.get("timeZone") or "UTC"))
        if source is None:
            return moment.date()  # a Windows zone name zoneinfo does not know: trust the fields
        moment = moment.replace(tzinfo=source)
    return to_wall_clock(moment, zone).date()


def graph_datetime(day: date, zone: Optional[tzinfo] = None) -> Dict[str, str]:
    """A date → ``dateTimeTimeZone`` at the instant of local midnight, expressed in UTC."""
    moment = local_midnight_utc(day, zone)
    return {"dateTime": moment.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}


def notes_of(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    content = text_of(body.get("content"))
    if str(body.get("contentType") or "").lower() == "html":
        return html_to_text(content)
    return content.strip()


def task_of(entry: Dict[str, Any], zone: Optional[tzinfo] = None) -> Task:
    """A Graph ``todoTask`` → Task."""
    completed_stamp = entry.get("completedDateTime")
    done = str(entry.get("status") or "") == "completed" or isinstance(completed_stamp, dict)
    importance = str(entry.get("importance") or "normal")
    return Task(
        summary=text_of(entry.get("title")),
        due=graph_date(entry.get("dueDateTime"), zone),
        start=graph_date(entry.get("startDateTime"), zone),
        completed=(graph_date(completed_stamp, zone) if done else None) or completion_date(done, None),
        priority=importance if importance in IMPORTANCE else "normal",
        notes=notes_of(entry.get("body")),
        categories=strings_of(entry.get("categories")),
        uid=str(entry.get("id") or ""),
    )


def payload_of(record: Task, zone: Optional[tzinfo] = None) -> Dict[str, Any]:
    """Task → the JSON body of a create (POST) or update (PATCH); ``null`` clears a field."""
    item = record.normalized()
    return {
        "title": item.summary or NO_SUBJECT,
        "body": {"content": item.notes, "contentType": "text"},
        "importance": item.priority,
        "categories": list(item.categories),
        "status": "completed" if item.is_completed else "notStarted",
        "dueDateTime": graph_datetime(item.due, zone) if item.due else None,
        "startDateTime": graph_datetime(item.start, zone) if item.start else None,
        "completedDateTime": graph_datetime(item.completed, zone) if is_real_completion(item.completed) else None,
    }


def _version(payload: Dict[str, Any]) -> Optional[str]:
    stamp = payload.get("lastModifiedDateTime")
    return stamp if isinstance(stamp, str) and stamp else None


# -- the store --------------------------------------------------------------------------
class MicrosoftTodoStore:
    """Store protocol over the tasks of one Microsoft To Do list."""

    name = "mstodo"

    def __init__(self, http: HttpClient, list_name: Optional[str] = None, log: Log = lambda _line: None,
                 zone: Optional[tzinfo] = None) -> None:
        self._http = http
        self._list_name = (list_name or "").strip()
        self._log = log
        self._zone = zone
        self._list_id: Optional[str] = None

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        entries = self._pages(f"{self._tasks_path()}?$top={PAGE_SIZE}", "listing")
        return tuple(Item(id=item_id_of(e, SERVICE, "listing"), record=task_of(e, self._zone), version=_version(e))
                     for e in entries)

    def create(self, record: Task) -> str:
        payload = self._json("POST", self._tasks_path(), "create", json_body=payload_of(record, self._zone))
        return item_id_of(payload, SERVICE, "create")

    def update(self, item_id: str, record: Task) -> Optional[str]:
        payload = self._json("PATCH", self._task_path(item_id), "update", json_body=payload_of(record, self._zone))
        return _version(payload)

    def delete(self, item_id: str) -> None:
        response = send(self._http, SERVICE, "DELETE", self._task_path(item_id), accept_errors=True)
        if response.status == 404:
            self._log(f"{SERVICE} task {item_id} was already gone")
        elif not response.ok:
            raise StoreError(f"{SERVICE} delete failed: HTTP {response.status} {response.text[:200].strip()}")

    # -- Graph calls ----------------------------------------------------------
    def _json(self, method: str, url: str, what: str, **kwargs: Any) -> Dict[str, Any]:
        return json_object(send(self._http, SERVICE, method, url, **kwargs), SERVICE, what)

    def _pages(self, first_url: str, what: str) -> List[Dict[str, Any]]:
        """Every entry of a paged collection (``value`` + ``@odata.nextLink``)."""
        entries: List[Dict[str, Any]] = []
        url: Optional[str] = first_url
        while url:
            payload = self._json("GET", url, what)
            entries.extend(e for e in (payload.get("value") or ()) if isinstance(e, dict))
            link = payload.get("@odata.nextLink")
            url = link if isinstance(link, str) and link else None
        return entries

    def _tasks_path(self) -> str:
        return f"/me/todo/lists/{quote(self._resolve_list(), safe='')}/tasks"

    def _task_path(self, item_id: str) -> str:
        if not isinstance(item_id, str) or not item_id.strip():
            raise StoreError(f"{SERVICE} needs a task id to update or delete")
        return f"{self._tasks_path()}/{quote(item_id, safe='')}"

    def _resolve_list(self) -> str:
        if self._list_id is None:
            self._list_id = choose_list(self._pages(f"/me/todo/lists?$top={PAGE_SIZE}", "list lookup"), self._list_name)
        return self._list_id


def choose_list(lists: List[Dict[str, Any]], wanted: str) -> str:
    """The id of the list called ``wanted`` (or its id), else of the well-known default list."""
    if wanted:
        key = wanted.casefold()
        for entry in lists:
            if str(entry.get("displayName") or "").casefold() == key or entry.get("id") == wanted:
                return item_id_of(entry, SERVICE, "list lookup")
        known = ", ".join(str(e.get("displayName") or "?") for e in lists) or "none"
        raise StoreError(f"{SERVICE} has no list called {wanted!r} (known: {known})")
    for entry in lists:
        if entry.get("wellknownListName") == DEFAULT_LIST_KIND:
            return item_id_of(entry, SERVICE, "list lookup")
    if lists:
        return item_id_of(lists[0], SERVICE, "list lookup")
    raise StoreError(f"{SERVICE} has no task lists; create one in the To Do app first")


# -- backend spec ---------------------------------------------------------------------
def tenant_of(account: Account) -> str:
    return (account.setting(TENANT_SETTING) or DEFAULT_TENANT).strip() or DEFAULT_TENANT


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Any:
    http = api_client(GRAPH_BASE, microsoft(tenant_of(account)), SCOPES, account, secrets, context)
    store = MicrosoftTodoStore(http, account.setting(LIST_SETTING), log=context.log)
    return wrap_completed(store, account, context.log)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(microsoft(tenant_of(account)), SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key="mstodo",
    title="Microsoft To Do",
    settings=oauth_settings("Microsoft") + (
        SettingSpec(TENANT_SETTING, f"Azure sign-in tenant: common (default), consumers, organizations or a tenant id",
                    required=False, default=DEFAULT_TENANT),
        SettingSpec(LIST_SETTING, "To Do list to sync, by name (default: the built-in Tasks list)", required=False),
    ),
    build=build,
    login=login,
    notes="Needs an Azure app registration (public client, redirect http://localhost) with Tasks.ReadWrite. "
          "The private flag does not travel.",
)

__all__ = ["MicrosoftTodoStore", "BACKEND", "build", "login", "task_of", "payload_of", "graph_date",
           "graph_datetime", "choose_list", "GRAPH_BASE", "SCOPES"]
