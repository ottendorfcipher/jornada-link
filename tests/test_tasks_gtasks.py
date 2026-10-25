import json
from datetime import date, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Task
from jornada.pim.tasks import UNKNOWN_COMPLETION_DATE
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.registry import BuildContext
from jornada.sync.tasks import gtasks
from jornada.sync.tasks.filters import CompletedFilter
from jornada.sync.tasks.gtasks import TASKS_BASE, GoogleTasksStore, choose_list, due_of, due_text, task_of
from jornada.webapi.http import HttpClient, HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

EASTERN = timezone(timedelta(hours=-4))
LISTS = "/tasks/v1/users/@me/lists"
TASKS = "/tasks/v1/lists/@default/tasks"
TASK_1 = {"id": "G1", "title": "Buy milk", "notes": "2%\r\nskim", "due": "2026-09-10T00:00:00.000Z",
          "status": "needsAction", "etag": '"e1"'}
TASK_2 = {"id": "G2", "title": "Done", "status": "completed", "completed": "2026-09-08T04:00:00.000Z",
          "updated": "2026-09-08T04:00:01.000Z"}
TASK_3 = {"id": "G3", "title": "Gone", "deleted": True}
TASK_4 = {"id": "G4", "title": "Old", "status": "completed"}


def paged(first, second, token):
    def handler(request):
        query = parse_qs(urlsplit(request.url).query)
        if query.get("pageToken") == [token]:
            return json_response(200, {"items": second})
        return json_response(200, {"items": first, "nextPageToken": token})
    return handler


def routes(extra=None):
    base = {
        ("GET", LISTS): paged([{"id": "L2", "title": "Chores"}], [{"id": "L3", "title": "Work"}], "lists2"),
        ("GET", TASKS): paged([TASK_1, TASK_2], [TASK_3, TASK_4], "tasks2"),
        ("POST", TASKS): json_response(200, {"id": "G9", "etag": '"e9"'}),
        ("PUT", f"{TASKS}/G1"): json_response(200, {"id": "G1", "updated": "2026-09-09T00:00:00.000Z"}),
    }
    return {**base, **(extra or {})}


def store_with(extra=None, tasklist="@default"):
    transport, seen = fake_transport(routes(extra))
    return GoogleTasksStore(HttpClient(TASKS_BASE, transport=transport), tasklist, zone=EASTERN), seen


def test_due_dates_only_carry_their_date_part():
    assert due_of("2026-09-10T00:00:00.000Z") == date(2026, 9, 10)
    assert due_of("2026-09-10T23:59:59.000Z") == date(2026, 9, 10)     # the time part is meaningless
    assert due_of("2026-13-10T00:00:00.000Z") is None and due_of(None) is None and due_of("2026") is None
    assert due_text(date(2026, 9, 10)) == "2026-09-10T00:00:00.000Z"


def test_task_mapping_has_no_priority_and_reads_completion_stamps_locally():
    assert task_of(TASK_1, EASTERN) == Task("Buy milk", due=date(2026, 9, 10), notes="2%\nskim", uid="G1")
    assert task_of(TASK_2, EASTERN) == Task("Done", completed=date(2026, 9, 8), uid="G2")
    assert task_of(TASK_4, EASTERN).completed == UNKNOWN_COMPLETION_DATE
    assert task_of({"id": "x", "title": "p", "status": "needsAction"}, EASTERN).priority == "normal"


def test_list_pages_through_the_default_list_and_skips_deleted_tasks():
    store, seen = store_with()
    items = store.list()
    assert [(i.id, i.version) for i in items] == [("G1", '"e1"'), ("G2", "2026-09-08T04:00:01.000Z"), ("G4", None)]
    assert [urlsplit(r.url).path for r in seen] == [TASKS, TASKS]
    query = parse_qs(urlsplit(seen[0].url).query)
    assert query == {"showCompleted": ["true"], "showHidden": ["true"], "maxResults": ["100"]}
    assert parse_qs(urlsplit(seen[1].url).query)["pageToken"] == ["tasks2"]
    assert store.name == "gtasks"


def test_named_task_lists_are_resolved_by_title_with_paging():
    store, seen = store_with({("GET", "/tasks/v1/lists/L3/tasks"): json_response(200, {"items": []})}, tasklist="work")
    assert store.list() == ()
    assert [urlsplit(r.url).path for r in seen] == [LISTS, LISTS, "/tasks/v1/lists/L3/tasks"]
    with pytest.raises(StoreError) as info:
        choose_list([{"id": "L2", "title": "Chores"}], "nope")
    assert "known: Chores" in str(info.value)
    assert choose_list([{"id": "L2", "title": "Chores"}], "L2") == "L2"


def test_create_update_delete_send_the_task_resource():
    lines = []
    store, seen = store_with({("DELETE", f"{TASKS}/G1"): HttpResponse(204), ("DELETE", f"{TASKS}/G2"): HttpResponse(404)})
    store._log = lines.append
    assert store.create(Task("Buy milk", due=date(2026, 9, 10), priority="high", notes="2%", categories=("lost",))) == "G9"
    assert json.loads(seen[-1].body) == {"title": "Buy milk", "notes": "2%", "status": "needsAction",
                                         "due": "2026-09-10T00:00:00.000Z", "completed": None}
    assert store.update("G1", Task("Done", completed=date(2026, 9, 8))) == "2026-09-09T00:00:00.000Z"
    assert seen[-1].method == "PUT" and json.loads(seen[-1].body) == {
        "id": "G1", "title": "Done", "notes": "", "status": "completed", "due": None,
        "completed": "2026-09-08T04:00:00.000Z"}
    store.update("G1", Task("", completed=UNKNOWN_COMPLETION_DATE))
    assert json.loads(seen[-1].body)["completed"] is None and json.loads(seen[-1].body)["title"] == "(no subject)"
    store.delete("G1")
    assert seen[-1].method == "DELETE" and urlsplit(seen[-1].url).path == f"{TASKS}/G1"
    store.delete("G2")
    assert lines == ["Google Tasks task G2 was already gone"]
    with pytest.raises(StoreError):
        store.update(" ", Task("x"))


def test_http_errors_become_store_errors():
    store, _ = store_with({("GET", TASKS): json_response(403, {"error": {"message": "insufficient scope"}})})
    with pytest.raises(StoreError) as info:
        store.list()
    assert "Google Tasks" in str(info.value) and "403" in str(info.value)
    odd, _ = store_with({("GET", TASKS): json_response(200, [1, 2])})
    with pytest.raises(StoreError):
        odd.list()
    failing_delete, _ = store_with({("DELETE", f"{TASKS}/G1"): json_response(403, {"error": "no"})})
    with pytest.raises(StoreError):
        failing_delete.delete("G1")


def test_backend_build_uses_the_oauth_token_and_settings(tmp_path):
    transport, seen = fake_transport(routes())
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None, http_transport=transport)
    secrets = {TOKEN_KEY: Token("tok", "rt", 4_000_000_000.0).to_dict()}
    hidden = gtasks.build(Account("g", "tasks", "gtasks", (("client_id", "cid"), ("completed", "hide"))), secrets, context)
    assert isinstance(hidden, CompletedFilter) and [i.id for i in hidden.list()] == ["G1"]
    assert seen[0].header("Authorization") == "Bearer tok"
    plain = gtasks.build(Account("g", "tasks", "gtasks", (("client_id", "cid"), ("tasklist", "Chores"))), secrets, context)
    assert isinstance(plain, GoogleTasksStore)
    assert gtasks.BACKEND.key == "gtasks" and gtasks.BACKEND.login is not None
    assert [s.key for s in gtasks.BACKEND.settings] == ["client_id", "client_secret", "token", "tasklist"]
    assert gtasks.BACKEND.missing_settings(Account("g", "tasks", "gtasks"), {}) == ("client_id", "token")


def test_login_needs_a_client_id_first(tmp_path):
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    with pytest.raises(AccountError):
        gtasks.login(Account("g", "tasks", "gtasks"), {}, context)
    assert gtasks.BACKEND.login is gtasks.login
