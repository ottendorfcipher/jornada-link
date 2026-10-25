"""Google Tasks backend: one task list ⇄ the device's tasks, through the Tasks API v1.

Google Tasks knows neither priorities, categories, start dates nor a private
flag, so those fields do not travel: every task reads back with priority
``normal``. A due date only carries its date part (the API discards the time),
so it is written as ``YYYY-MM-DDT00:00:00.000Z`` and read from the first ten
characters; the completion stamp is a real instant and is read in local time.
"""
from __future__ import annotations

from datetime import date, tzinfo
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ...pim.models import Task
from ...webapi.http import HttpClient
from ...webapi.oauth import GOOGLE
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import (NO_SUBJECT, Log, completion_date, is_real_completion, item_id_of, json_object,
                     local_midnight_utc, send, text_of)
from .filters import wrap_completed

SERVICE = "Google Tasks"
TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
SCOPES = ("https://www.googleapis.com/auth/tasks",)
DEFAULT_TASKLIST = "@default"
TASKLIST_SETTING = "tasklist"
PAGE_SIZE = 100
LIST_PARAMS = {"showCompleted": "true", "showHidden": "true", "maxResults": PAGE_SIZE}


# -- value mapping --------------------------------------------------------------------
def due_of(value: Any) -> Optional[date]:
    """``due`` is an RFC 3339 stamp whose time part is meaningless: the date part is the date."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def due_text(day: date) -> str:
    return f"{day.isoformat()}T00:00:00.000Z"


def task_of(entry: Dict[str, Any], zone: Optional[tzinfo] = None) -> Task:
    """A Tasks API ``task`` → Task (priority is always normal: the API has none)."""
    done = str(entry.get("status") or "") == "completed"
    return Task(
        summary=text_of(entry.get("title")),
        due=due_of(entry.get("due")),
        completed=completion_date(done, entry.get("completed"), zone),
        notes=text_of(entry.get("notes")).strip(),
        uid=str(entry.get("id") or ""),
    )


def payload_of(record: Task, zone: Optional[tzinfo] = None) -> Dict[str, Any]:
    """Task → the JSON body of an insert or update; ``null`` clears a field."""
    item = record.normalized()
    completed = item.completed
    return {
        "title": item.summary or NO_SUBJECT,
        "notes": item.notes,
        "status": "completed" if item.is_completed else "needsAction",
        "due": due_text(item.due) if item.due else None,
        "completed": (local_midnight_utc(completed, zone).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                      if is_real_completion(completed) else None),
    }


def _version(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("etag", "updated"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# -- the store ----------------------------------------------------------------------------
class GoogleTasksStore:
    """Store protocol over the tasks of one Google Tasks list."""

    name = "gtasks"

    def __init__(self, http: HttpClient, tasklist: str = DEFAULT_TASKLIST, log: Log = lambda _line: None,
                 zone: Optional[tzinfo] = None) -> None:
        self._http = http
        self._tasklist = (tasklist or "").strip() or DEFAULT_TASKLIST
        self._log = log
        self._zone = zone
        self._list_id: Optional[str] = None

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        entries = self._pages(self._tasks_path(), "listing", LIST_PARAMS)
        return tuple(Item(id=item_id_of(e, SERVICE, "listing"), record=task_of(e, self._zone), version=_version(e))
                     for e in entries if not e.get("deleted"))

    def create(self, record: Task) -> str:
        payload = self._json("POST", self._tasks_path(), "create", json_body=payload_of(record, self._zone))
        return item_id_of(payload, SERVICE, "create")

    def update(self, item_id: str, record: Task) -> Optional[str]:
        body = {"id": item_id, **payload_of(record, self._zone)}
        return _version(self._json("PUT", self._task_path(item_id), "update", json_body=body))

    def delete(self, item_id: str) -> None:
        response = send(self._http, SERVICE, "DELETE", self._task_path(item_id), accept_errors=True)
        if response.status == 404:
            self._log(f"{SERVICE} task {item_id} was already gone")
        elif not response.ok:
            raise StoreError(f"{SERVICE} delete failed: HTTP {response.status} {response.text[:200].strip()}")

    # -- API calls ------------------------------------------------------------
    def _json(self, method: str, url: str, what: str, **kwargs: Any) -> Dict[str, Any]:
        return json_object(send(self._http, SERVICE, method, url, **kwargs), SERVICE, what)

    def _pages(self, path: str, what: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Every entry of a paged collection (``items`` + ``nextPageToken``)."""
        entries: List[Dict[str, Any]] = []
        token: Optional[str] = None
        while True:
            payload = self._json("GET", path, what, params={**params, "pageToken": token})
            entries.extend(e for e in (payload.get("items") or ()) if isinstance(e, dict))
            next_token = payload.get("nextPageToken")
            token = next_token if isinstance(next_token, str) and next_token else None
            if not token:
                return entries

    def _tasks_path(self) -> str:
        return f"/lists/{quote(self._resolve_list(), safe='@')}/tasks"

    def _task_path(self, item_id: str) -> str:
        if not isinstance(item_id, str) or not item_id.strip():
            raise StoreError(f"{SERVICE} needs a task id to update or delete")
        return f"{self._tasks_path()}/{quote(item_id, safe='')}"

    def _resolve_list(self) -> str:
        if self._list_id is None:
            if self._tasklist == DEFAULT_TASKLIST:
                self._list_id = DEFAULT_TASKLIST
            else:
                lists = self._pages("/users/@me/lists", "list lookup", {"maxResults": PAGE_SIZE})
                self._list_id = choose_list(lists, self._tasklist)
        return self._list_id


def choose_list(lists: List[Dict[str, Any]], wanted: str) -> str:
    """The id of the task list titled ``wanted`` (or whose id it is)."""
    key = wanted.casefold()
    for entry in lists:
        if str(entry.get("title") or "").casefold() == key or entry.get("id") == wanted:
            return item_id_of(entry, SERVICE, "list lookup")
    known = ", ".join(str(e.get("title") or "?") for e in lists) or "none"
    raise StoreError(f"{SERVICE} has no task list called {wanted!r} (known: {known})")


# -- backend spec ---------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Any:
    http = api_client(TASKS_BASE, GOOGLE, SCOPES, account, secrets, context)
    store = GoogleTasksStore(http, account.setting(TASKLIST_SETTING) or DEFAULT_TASKLIST, log=context.log)
    return wrap_completed(store, account, context.log)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(GOOGLE, SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key="gtasks",
    title="Google Tasks",
    settings=oauth_settings("Google") + (
        SettingSpec(TASKLIST_SETTING, f"task list to sync, by title (default {DEFAULT_TASKLIST}: the primary list)",
                    required=False, default=DEFAULT_TASKLIST),
    ),
    build=build,
    login=login,
    notes="Google Tasks has no priorities, categories, start dates or private flag: those fields do not travel "
          "and every task reads back with priority normal.",
)

__all__ = ["GoogleTasksStore", "BACKEND", "build", "login", "task_of", "payload_of", "due_of", "due_text",
           "choose_list", "TASKS_BASE", "SCOPES"]
