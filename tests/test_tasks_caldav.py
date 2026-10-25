import datetime as dt
from datetime import date, timedelta, timezone

import pytest

from jornada.pim.models import Task
from jornada.pim.tasks import UNKNOWN_COMPLETION_DATE
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.registry import BuildContext
from jornada.sync.tasks import caldav, icalmap
from jornada.sync.tasks.caldav import CalDavTasksStore, choose_collection, path_of
from jornada.sync.tasks.filters import CompletedFilter
from jornada.sync.tasks.icalmap import calendar_with, from_vtodo, to_vtodo
from jornada.webapi import dav, ical
from tests.fake_dav import NS_CALDAV, PASSWORD, USERNAME, FakeDavHandler, FakeDavServer, Item, _multistatus

EASTERN = timezone(timedelta(hours=-4))


def crlf(text):
    return text.replace("\n", "\r\n")


TODO_1 = crlf("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Nextcloud Tasks//EN
BEGIN:VTODO
UID:t1
DTSTAMP:20260901T000000Z
CREATED:20260830T120000Z
SUMMARY:Buy milk
DUE;VALUE=DATE:20260910
DTSTART;VALUE=DATE:20260901
PRIORITY:1
DESCRIPTION:2%\\nskim
CATEGORIES:Errands,Home
CLASS:PRIVATE
STATUS:NEEDS-ACTION
X-NEXTCLOUD-COLOR:blue
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
END:VALARM
END:VTODO
END:VCALENDAR
""")
TODO_2 = crlf("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Tasks.org//EN
BEGIN:VTODO
UID:t2
DTSTAMP:20260901T000000Z
SUMMARY:Done thing
DUE:20260911T035900Z
COMPLETED:20260908T150000Z
STATUS:COMPLETED
PERCENT-COMPLETE:100
PRIORITY:9
END:VTODO
END:VCALENDAR
""")
TODO_3 = crlf("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Radicale//EN
BEGIN:VTODO
UID:t3
DTSTAMP:20260901T000000Z
SUMMARY:Old
STATUS:COMPLETED
END:VTODO
END:VCALENDAR
""")
BROKEN_TODO = crlf("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VTODO\nUID:j\nno colon on this line\nEND:VTODO\nEND:VCALENDAR\n")


class TaskDavHandler(FakeDavHandler):
    """The shared fake only answers calendar-query with VEVENTs; this one honours the requested component."""

    def _calendar_query(self, root):
        wanted = next((el.get("name") for el in root.iter(f"{{{NS_CALDAV}}}comp-filter") if el.get("name") != "VCALENDAR"), "VEVENT")
        chosen = [(h, i) for h, i in self._members(self.path) if f"BEGIN:{wanted}" in i.data]
        self._xml(207, _multistatus("".join(self._data_response(h, i) for h, i in chosen)))


@pytest.fixture
def server():
    srv = FakeDavServer()
    srv.RequestHandlerClass = TaskDavHandler
    srv.items["/cal/tasks/t1.ics"] = Item('"etag-t1"', TODO_1, "text/calendar; charset=utf-8")
    srv.items["/cal/tasks/t2.ics"] = Item('"etag-t2"', TODO_2, "text/calendar; charset=utf-8")
    srv.items["/cal/tasks/t3.ics"] = Item('"etag-t3"', TODO_3, "text/calendar; charset=utf-8")
    srv.items["/cal/tasks/j.ics"] = Item('"etag-j"', BROKEN_TODO, "text/calendar; charset=utf-8")
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def store(server):
    client = dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5)
    uids = iter(["new-1", "new-2", "new-3"])
    return CalDavTasksStore(client, calendar="Tasks", zone=EASTERN, new_uid=lambda: next(uids))


# -- icalmap ---------------------------------------------------------------------------------

def test_from_vtodo_reads_dates_priority_completion_and_class():
    task = from_vtodo(ical.parse(TODO_1).find("VTODO"), EASTERN)
    assert task == Task("Buy milk", due=date(2026, 9, 10), start=date(2026, 9, 1), priority="high", notes="2%\nskim",
                        categories=("Errands", "Home"), private=True, uid="t1")
    done = from_vtodo(ical.parse(TODO_2).find("VTODO"), EASTERN)
    assert done == Task("Done thing", due=date(2026, 9, 10), completed=date(2026, 9, 8), priority="low", uid="t2")
    old = from_vtodo(ical.parse(TODO_3).find("VTODO"), EASTERN)
    assert old.completed == UNKNOWN_COMPLETION_DATE and old.priority == "normal" and not old.private


@pytest.mark.parametrize("body,expected", [
    ("STATUS:COMPLETED", UNKNOWN_COMPLETION_DATE),
    ("PERCENT-COMPLETE:100", UNKNOWN_COMPLETION_DATE),
    ("PERCENT-COMPLETE:50", None),
    ("STATUS:IN-PROCESS", None),
    ("COMPLETED:20260908T030000Z", date(2026, 9, 7)),         # 03:00Z is the previous evening in Eastern time
    ("COMPLETED;VALUE=DATE:20260908", date(2026, 9, 8)),
    ("COMPLETED:garbage", UNKNOWN_COMPLETION_DATE),
])
def test_completion_variants(body, expected):
    todo = ical.parse(f"BEGIN:VTODO\nUID:x\nSUMMARY:s\n{body}\nEND:VTODO")
    assert from_vtodo(todo, EASTERN).completed == expected


@pytest.mark.parametrize("value,expected", [("0", "normal"), ("1", "high"), ("4", "high"), ("5", "normal"),
                                            ("6", "low"), ("9", "low"), ("x", "normal"), (None, "normal")])
def test_priority_scale(value, expected):
    assert icalmap.priority_name(value) == expected


def test_to_vtodo_round_trips_and_keeps_foreign_properties_and_alarms():
    original = ical.parse(TODO_1).find("VTODO")
    task = Task("Buy oat milk", due=date(2026, 9, 12), start=date(2026, 9, 2), completed=date(2026, 9, 11),
                priority="low", notes="a, b; c", categories=("Errands",), private=False)
    todo = to_vtodo(task, "t1", base=original, zone=EASTERN, dtstamp=dt.datetime(2026, 1, 1, tzinfo=timezone.utc))
    text = ical.serialize(calendar_with(todo, ical.parse(TODO_1)))
    assert "DUE;VALUE=DATE:20260912" in text and "DTSTART;VALUE=DATE:20260902" in text
    assert "COMPLETED:20260911T040000Z" in text and "STATUS:COMPLETED" in text and "PERCENT-COMPLETE:100" in text
    assert "PRIORITY:9" in text and "CLASS:" not in text and "DESCRIPTION:a\\, b\\; c" in text
    assert "X-NEXTCLOUD-COLOR:blue" in text and "CREATED:20260830T120000Z" in text and "TRIGGER:-PT15M" in text
    back = from_vtodo(ical.parse(text).find("VTODO"), EASTERN)
    assert back == Task("Buy oat milk", due=date(2026, 9, 12), start=date(2026, 9, 2), completed=date(2026, 9, 11),
                        priority="low", notes="a, b; c", categories=("Errands",), uid="t1")
    fresh = to_vtodo(Task(" ", completed=UNKNOWN_COMPLETION_DATE, private=True), "u9")
    fresh_text = ical.serialize(fresh)
    assert "SUMMARY:(no subject)" in fresh_text and "COMPLETED:" not in fresh_text and "CLASS:PRIVATE" in fresh_text
    assert "PRIORITY:" not in fresh_text and from_vtodo(fresh).completed == UNKNOWN_COMPLETION_DATE
    assert "STATUS:NEEDS-ACTION" in ical.serialize(to_vtodo(Task("open"), "u1"))
    with pytest.raises(ical.IcalError):
        from_vtodo(ical.parse("BEGIN:VEVENT\nUID:x\nEND:VEVENT"))


# -- the store against the fake server ----------------------------------------------------------

def test_collection_choice():
    work = dav.Collection("https://h/cal/work/", "Work", None, ("VEVENT", "VTODO"), "calendar")
    tasks = dav.Collection("https://h/cal/tasks/", "Tasks", None, ("VTODO",), "calendar")
    events = dav.Collection("https://h/cal/ev/", "Events", None, ("VEVENT",), "calendar")
    silent = dav.Collection("https://h/cal/x/", "Unknown", None, (), "calendar")
    assert choose_collection((events, work, tasks), None) is work
    assert choose_collection((events, silent), None) is silent
    assert choose_collection((work, tasks), "tasks") is tasks
    assert choose_collection((work, tasks), "/cal/tasks/") is tasks
    assert choose_collection((work, tasks), "https://h/cal/work") is work
    with pytest.raises(StoreError) as info:
        choose_collection((work, tasks), "Nope")
    assert "known: Work, Tasks" in str(info.value)
    with pytest.raises(StoreError):
        choose_collection((events,), None)
    assert path_of("https://h/cal/x.ics") == "/cal/x.ics" and path_of("/cal/y.ics") == "/cal/y.ics"


def test_list_maps_vtodos_and_skips_other_objects(server, store):
    lines = []
    store._log = lines.append
    items = store.list()
    assert [(i.id, i.version) for i in items] == [("/cal/tasks/t1.ics", '"etag-t1"'), ("/cal/tasks/t2.ics", '"etag-t2"'),
                                                  ("/cal/tasks/t3.ics", '"etag-t3"')]
    assert items[0].record.summary == "Buy milk" and items[1].record.completed == date(2026, 9, 8)
    assert any("skipping /cal/tasks/j.ics" in line for line in lines) and any("'Tasks'" in line for line in lines)
    report = [(p, h) for verb, p, h in server.requests if verb == "REPORT"][-1]
    assert report[0] == "/cal/tasks/" and report[1].get("Depth") == "1"
    default = CalDavTasksStore(dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5))
    assert default.collection_href() == server.base_url + "cal/work/"      # first calendar accepting VTODO


def test_create_update_delete_lifecycle(server, store):
    store.list()
    task = Task("Call mum", due=date(2026, 9, 12), priority="high", categories=("Family",))
    item_id = store.create(task)
    assert item_id == "/cal/tasks/new-1.ics" and "/cal/tasks/new-1.ics" in server.items
    put = [(p, h) for verb, p, h in server.requests if verb == "PUT"][-1]
    assert put[1].get("If-None-Match") == "*" and put[1].get("Content-Type").startswith("text/calendar")
    stored = ical.parse(server.items[item_id].data)
    assert from_vtodo(stored.find("VTODO"), EASTERN) == Task("Call mum", due=date(2026, 9, 12), priority="high",
                                                             categories=("Family",), uid="new-1")
    (created,) = [i for i in store.list() if i.id == item_id]
    assert created.version == server.items[item_id].etag

    new_etag = store.update("/cal/tasks/t1.ics", Task("Buy milk", due=date(2026, 9, 10), completed=date(2026, 9, 9)))
    put = [(p, h) for verb, p, h in server.requests if verb == "PUT"][-1]
    assert put[0] == "/cal/tasks/t1.ics" and put[1].get("If-Match") == '"etag-t1"' and new_etag == server.items[put[0]].etag
    updated = ical.parse(server.items["/cal/tasks/t1.ics"].data)
    assert updated.find("VTODO").value("UID") == "t1" and updated.find("VTODO").value("X-NEXTCLOUD-COLOR") == "blue"
    assert updated.find("VALARM") is not None and updated.find("VTODO").value("STATUS") == "COMPLETED"

    store.delete("/cal/tasks/t2.ics")
    assert "/cal/tasks/t2.ics" not in server.items
    lines = []
    store._log = lines.append
    store.delete("/cal/tasks/t2.ics")
    assert lines == ["/cal/tasks/t2.ics was already gone from the server"]


def test_conflicts_and_server_errors_become_store_errors(server, store):
    store.list()
    server.items["/cal/tasks/t1.ics"] = Item('"etag-t1-changed"', TODO_1, "text/calendar")
    with pytest.raises(StoreError) as info:
        store.update("/cal/tasks/t1.ics", Task("Buy milk"))
    assert "changed on the server" in str(info.value)
    with pytest.raises(StoreError):
        store.delete("/cal/tasks/t1.ics")
    fresh = CalDavTasksStore(dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5), calendar="Tasks", zone=EASTERN)
    assert fresh.update("/cal/tasks/t1.ics", Task("Fetched first")) == server.items["/cal/tasks/t1.ics"].etag
    assert "SUMMARY:Fetched first" in server.items["/cal/tasks/t1.ics"].data
    with pytest.raises(StoreError):
        fresh.update("/cal/tasks/missing.ics", Task("x"))
    with pytest.raises(StoreError):
        fresh.update("/cal/tasks/j.ics", Task("x"))                   # unreadable object
    bad = CalDavTasksStore(dav.DavClient(server.base_url, USERNAME, "wrong", timeout=5))
    with pytest.raises(StoreError) as info:
        bad.list()
    assert "wrong" not in str(info.value)


def test_backend_build_reads_settings_and_secrets(server, tmp_path):
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    account = Account("c", "tasks", "caldav", (("url", server.base_url), ("username", USERNAME), ("calendar", "Tasks"),
                                              ("completed", "hide")))
    store = caldav.build(account, {"password": PASSWORD}, context)
    assert isinstance(store, CompletedFilter) and [i.id for i in store.list()] == ["/cal/tasks/t1.ics"]
    plain = caldav.build(account.with_setting("completed", "keep"), {"password": PASSWORD}, context)
    assert isinstance(plain, CalDavTasksStore)
    with pytest.raises(StoreError):
        caldav.build(Account("c", "tasks", "caldav", (("url", "not a url"),)), {}, context)
    assert caldav.BACKEND.key == "caldav" and caldav.BACKEND.login is None
    assert [s.key for s in caldav.BACKEND.settings] == ["url", "username", "password", "calendar"]
    assert caldav.BACKEND.missing_settings(Account("c", "tasks", "caldav"), {}) == ("url", "username", "password")
    assert caldav.BACKEND.missing_settings(account, {"password": "x"}) == ()


def failing(transport, methods):
    """A DAV transport that answers ``methods`` with HTTP 500 and passes everything else through."""
    def send(request):
        if request.method in methods:
            return dav.Response(500, (), b"<html>boom</html>", request.url)
        return transport(request)
    return send


def test_server_errors_on_report_put_and_delete_become_store_errors(server):
    flaky = dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5,
                          transport=failing(dav.urllib_transport(5.0), ("REPORT", "PUT")))
    store = CalDavTasksStore(flaky, calendar="Tasks", zone=EASTERN)
    with pytest.raises(StoreError) as info:
        store.list()
    assert "CalDAV listing failed" in str(info.value) and "500" in str(info.value)
    with pytest.raises(StoreError) as info:
        store.create(Task("x"))
    assert "could not create 'x'" in str(info.value) and "500" in str(info.value)
    unauthorized = CalDavTasksStore(dav.DavClient(server.base_url, USERNAME, "wrong", timeout=5))
    with pytest.raises(StoreError) as info:
        unauthorized.delete("/cal/tasks/t1.ics")
    assert "delete failed" in str(info.value) and "wrong" not in str(info.value)
    assert "/cal/tasks/t1.ics" in server.items


def test_objects_without_a_vtodo_are_skipped_or_refused(server, store):
    event = crlf("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:e\nDTSTAMP:20260901T000000Z\nDTSTART:20260910T090000Z\n"
                 "SUMMARY:Not a task\nX-NOTE:BEGIN:VTODO\nEND:VEVENT\nEND:VCALENDAR\n")
    server.items["/cal/tasks/e.ics"] = Item('"etag-e"', event, "text/calendar; charset=utf-8")
    lines = []
    store._log = lines.append
    assert [i.id for i in store.list()] == ["/cal/tasks/t1.ics", "/cal/tasks/t2.ics", "/cal/tasks/t3.ics"]
    assert any("skipping /cal/tasks/e.ics: no VTODO component" in line for line in lines)
    with pytest.raises(StoreError) as info:
        store.update("/cal/work/a.ics", Task("x"))             # fetched from the server: an event, not a task
    assert "holds no VTODO" in str(info.value)
    todo = ical.parse("BEGIN:VTODO\nUID:x\nSUMMARY:s\nEND:VTODO")
    assert icalmap.first_vtodo(todo) is todo and icalmap.first_vtodo(ical.vcalendar(todo)).value("UID") == "x"
