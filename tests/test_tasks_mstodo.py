import json
from datetime import date, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from jornada.pim.models import Task
from jornada.pim.tasks import UNKNOWN_COMPLETION_DATE
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.registry import BuildContext
from jornada.sync.tasks import mstodo
from jornada.sync.tasks.filters import CompletedFilter
from jornada.sync.tasks.mstodo import GRAPH_BASE, MicrosoftTodoStore, choose_list, graph_date, graph_datetime, task_of
from jornada.webapi.http import HttpClient, HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

EASTERN = timezone(timedelta(hours=-4))
LISTS = "/v1.0/me/todo/lists"
TASKS = f"{LISTS}/L1/tasks"


def utc(day, hour=4):
    return {"dateTime": f"{day}T{hour:02d}:00:00.0000000", "timeZone": "UTC"}


TASK_1 = {"id": "T1", "title": "Buy milk", "body": {"content": "<div>2%<br>skim</div>", "contentType": "html"},
          "dueDateTime": utc("2026-09-10"), "startDateTime": utc("2026-09-01"), "status": "notStarted",
          "importance": "high", "categories": ["Errands"], "lastModifiedDateTime": "2026-09-06T10:00:00Z"}
TASK_2 = {"id": "T2", "title": "Done", "body": {"content": "plain\r\ntext", "contentType": "text"},
          "status": "completed", "completedDateTime": utc("2026-09-08"), "importance": "low"}
TASK_3 = {"id": "T3", "title": "Old", "status": "completed", "importance": "weird",
          "dueDateTime": {"dateTime": "2026-09-12T00:00:00", "timeZone": "Pacific Standard Time"}}


def paged(first, second, marker):
    def handler(request):
        if marker in urlsplit(request.url).query:
            return json_response(200, {"value": second})
        return json_response(200, {"value": first, "@odata.nextLink": f"{GRAPH_BASE}{urlsplit(request.url).path[5:]}?$skiptoken={marker}"})
    return handler


def routes(extra=None):
    base = {
        ("GET", LISTS): paged([{"id": "L0", "displayName": "Groceries"}],
                              [{"id": "L1", "displayName": "Tasks", "wellknownListName": "defaultList"}], "lists2"),
        ("GET", TASKS): paged([TASK_1, TASK_2], [TASK_3], "tasks2"),
        ("POST", TASKS): json_response(201, {"id": "T9", "lastModifiedDateTime": "v9"}),
        ("PATCH", f"{TASKS}/T1"): json_response(200, {"id": "T1", "lastModifiedDateTime": "v2"}),
    }
    return {**base, **(extra or {})}


def store_with(extra=None, list_name=None):
    transport, seen = fake_transport(routes(extra))
    return MicrosoftTodoStore(HttpClient(GRAPH_BASE, transport=transport), list_name, zone=EASTERN), seen


def test_graph_dates_are_read_in_the_local_zone_and_written_as_local_midnight():
    assert graph_date(utc("2026-09-10"), EASTERN) == date(2026, 9, 10)
    assert graph_date({"dateTime": "2026-09-11T03:59:00.0000000", "timeZone": "UTC"}, EASTERN) == date(2026, 9, 10)
    assert graph_date({"dateTime": "2026-09-12T00:00:00", "timeZone": "Pacific Standard Time"}, EASTERN) == date(2026, 9, 12)
    assert graph_date({"dateTime": "2026-09-12T00:30:00", "timeZone": "Europe/Berlin"}, timezone.utc) == date(2026, 9, 11)
    assert graph_date({"dateTime": "nope"}, EASTERN) is None and graph_date("2026-09-10", EASTERN) is None
    assert graph_datetime(date(2026, 9, 10), EASTERN) == utc("2026-09-10")
    assert graph_datetime(date(2026, 9, 10), timezone.utc) == utc("2026-09-10", 0)


def test_task_mapping_covers_html_bodies_completion_and_importance():
    assert task_of(TASK_1, EASTERN) == Task("Buy milk", due=date(2026, 9, 10), start=date(2026, 9, 1), priority="high",
                                            notes="2%\nskim", categories=("Errands",), uid="T1")
    assert task_of(TASK_2, EASTERN) == Task("Done", completed=date(2026, 9, 8), priority="low", notes="plain\ntext", uid="T2")
    old = task_of(TASK_3, EASTERN)
    assert old.completed == UNKNOWN_COMPLETION_DATE and old.priority == "normal" and old.due == date(2026, 9, 12)


def test_list_resolves_the_default_list_and_follows_paging():
    store, seen = store_with()
    items = store.list()
    assert [i.id for i in items] == ["T1", "T2", "T3"] and items[0].version == "2026-09-06T10:00:00Z"
    assert [urlsplit(r.url).path for r in seen] == [LISTS, LISTS, TASKS, TASKS]
    assert "$top=100" in seen[0].url and "skiptoken=lists2" in seen[1].url and "skiptoken=tasks2" in seen[3].url
    assert store.name == "mstodo"


def test_named_list_is_matched_case_insensitively_or_reported():
    store, seen = store_with(list_name="groceries")
    with pytest.raises(StoreError):
        store.list()            # no route for L0's tasks → 404 → StoreError
    assert urlsplit(seen[-1].url).path == f"{LISTS}/L0/tasks"
    assert choose_list([{"id": "L0", "displayName": "A"}], "L0") == "L0"
    with pytest.raises(StoreError) as info:
        choose_list([{"id": "L0", "displayName": "Groceries"}, {"id": "L1", "displayName": "Tasks"}], "nope")
    assert "known: Groceries, Tasks" in str(info.value)
    assert choose_list([{"id": "X", "displayName": "Only"}], "") == "X"
    with pytest.raises(StoreError):
        choose_list([], "")


def test_create_update_delete_send_graph_payloads():
    lines = []
    store, seen = store_with({("DELETE", f"{TASKS}/T1"): HttpResponse(204), ("DELETE", f"{TASKS}/T2"): HttpResponse(404)})
    store._log = lines.append
    task = Task("Buy milk", due=date(2026, 9, 10), start=date(2026, 9, 1), completed=date(2026, 9, 8), priority="high",
                notes="2%", categories=("Errands", "Home"))
    assert store.create(task) == "T9"
    body = json.loads(seen[-1].body)
    assert body == {"title": "Buy milk", "body": {"content": "2%", "contentType": "text"}, "importance": "high",
                    "categories": ["Errands", "Home"], "status": "completed", "dueDateTime": utc("2026-09-10"),
                    "startDateTime": utc("2026-09-01"), "completedDateTime": utc("2026-09-08")}
    assert seen[-1].header("Content-Type") == "application/json"
    assert store.update("T1", Task("", completed=UNKNOWN_COMPLETION_DATE)) == "v2"
    body = json.loads(seen[-1].body)
    assert body["title"] == "(no subject)" and body["dueDateTime"] is None and body["completedDateTime"] is None
    assert body["status"] == "completed" and seen[-1].method == "PATCH"
    store.delete("T1")
    assert seen[-1].method == "DELETE" and urlsplit(seen[-1].url).path == f"{TASKS}/T1"
    store.delete("T2")
    assert lines == ["Microsoft To Do task T2 was already gone"]
    with pytest.raises(StoreError):
        store.delete("")


def test_http_errors_become_store_errors():
    store, _ = store_with({("GET", TASKS): json_response(403, {"error": {"message": "Forbidden"}})})
    with pytest.raises(StoreError) as info:
        store.list()
    assert "Microsoft To Do" in str(info.value) and "403" in str(info.value)
    broken, _ = store_with({("GET", TASKS): HttpResponse(200, (), b"not json")})
    with pytest.raises(StoreError):
        broken.list()
    odd, _ = store_with({("POST", TASKS): json_response(201, {"nope": 1})})
    with pytest.raises(StoreError):
        odd.create(Task("x"))


def test_backend_build_uses_the_oauth_token_and_settings(tmp_path):
    transport, seen = fake_transport(routes())
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None, http_transport=transport)
    secrets = {TOKEN_KEY: Token("tok", "rt", 4_000_000_000.0).to_dict()}
    account = Account("m", "tasks", "mstodo", (("client_id", "cid"), ("tenant", "consumers"), ("completed", "hide")))
    store = mstodo.build(account, secrets, context)
    assert isinstance(store, CompletedFilter) and [i.id for i in store.list()] == ["T1"]
    assert seen[0].header("Authorization") == "Bearer tok"
    assert isinstance(mstodo.build(Account("m", "tasks", "mstodo", (("client_id", "cid"),)), secrets, context), MicrosoftTodoStore)
    assert mstodo.BACKEND.key == "mstodo" and mstodo.BACKEND.login is not None
    assert [s.key for s in mstodo.BACKEND.settings] == ["client_id", "client_secret", "token", "tenant", "list"]
    assert mstodo.BACKEND.missing_settings(Account("m", "tasks", "mstodo"), {}) == ("client_id", "token")
    assert mstodo.tenant_of(account) == "consumers" and mstodo.tenant_of(Account("m", "tasks", "mstodo")) == "common"


def test_delete_errors_dates_without_a_time_and_login_preconditions(tmp_path):
    store, _ = store_with({("DELETE", f"{TASKS}/T1"): json_response(403, {"error": {"message": "Forbidden"}})})
    with pytest.raises(StoreError) as info:
        store.delete("T1")
    assert "delete failed" in str(info.value) and "403" in str(info.value)
    assert graph_date({"dateTime": "2026-09-10"}, EASTERN) == date(2026, 9, 10)     # a bare date is taken as is
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    with pytest.raises(AccountError):
        mstodo.login(Account("m", "tasks", "mstodo"), {}, context)
    assert mstodo.BACKEND.login is mstodo.login
