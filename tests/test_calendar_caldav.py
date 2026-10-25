"""The CalDAV backend against the in-process fake server: discovery, listing inside the window,
conditional create / update / delete with ETags, conflicts and the backend spec."""
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from jornada.pim.models import Appointment
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.calendar import caldav
from jornada.sync.calendar.caldav import (CONTENT_TYPE, TRANSPORT_KEY, CalDavCalendarStore, KnownObject, UnreadableObject,
                                          choose_collection, new_uid, object_path, read_object)
from jornada.sync.calendar.icalmap import from_vevent
from jornada.sync.calendar.window import WindowedStore
from jornada.sync.registry import BuildContext
from jornada.webapi import dav, ical
from tests.fake_dav import NS_CALDAV, PASSWORD, USERNAME, FakeDavHandler, FakeDavServer, Item, _multistatus

START, END = datetime(2026, 1, 30), datetime(2027, 3, 2)
WORK = "/cal/work/"
DENTIST = Appointment("Dentist", datetime(2026, 9, 7, 9, 30), datetime(2026, 9, 7, 10, 15), location="Clinic",
                      notes="bring card", categories=("Health",), busy_status="tentative", private=True,
                      reminder_minutes=15)


def crlf(text):
    return text.replace("\n", "\r\n")


def vcalendar(*lines):
    body = "\n".join(lines)
    return crlf(f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//fake//EN\nBEGIN:VEVENT\n{body}\nEND:VEVENT\nEND:VCALENDAR\n")


DENTIST_ICS = vcalendar("UID:dentist", "DTSTAMP:20260301T000000Z", "DTSTART:20260907T073000Z", "DTEND:20260907T081500Z",
                        "SUMMARY:Dentist", "LOCATION:Clinic", "DESCRIPTION:bring card", "CATEGORIES:Health",
                        "CLASS:PRIVATE", "TRANSP:OPAQUE", "X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE",
                        "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M", "END:VALARM")
BOARD_ICS = vcalendar("UID:board", "DTSTAMP:20260301T000000Z", "DTSTART;VALUE=DATE:20260910", "DTEND;VALUE=DATE:20260912",
                      "SUMMARY:Board", "TRANSP:TRANSPARENT")
STANDUP_ICS = vcalendar("UID:standup", "DTSTAMP:20260301T000000Z", "DTSTART:20260908T070000Z", "DTEND:20260908T071500Z",
                        "SUMMARY:Standup", "RRULE:FREQ=WEEKLY")
OVERRIDE_ICS = vcalendar("UID:standup", "DTSTAMP:20260301T000000Z", "RECURRENCE-ID:20260915T070000Z",
                         "DTSTART:20260915T080000Z", "SUMMARY:Standup (moved)")
OLD_ICS = vcalendar("UID:old", "DTSTAMP:20200101T000000Z", "DTSTART:20200101T090000Z", "DTEND:20200101T100000Z",
                    "SUMMARY:Ancient")
NO_UID_ICS = vcalendar("DTSTAMP:20260301T000000Z", "DTSTART:20260920T090000Z", "DTEND:20260920T100000Z", "SUMMARY:Anonymous")
BROKEN_ICS = crlf("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:j\nno colon on this line\nEND:VEVENT\nEND:VCALENDAR\n")
EXTRA_OBJECTS = (("dentist", DENTIST_ICS), ("board", BOARD_ICS), ("standup", STANDUP_ICS), ("override", OVERRIDE_ICS),
                 ("old", OLD_ICS), ("nouid", NO_UID_ICS), ("broken", BROKEN_ICS))


class CalendarDavHandler(FakeDavHandler):
    """The stock fake keeps objects whose DTSTART lies in the range and cannot compare all-day dates;
    this one keeps every object overlapping the range and hands unreadable ones to the client."""

    def _calendar_query(self, root):
        time_range = root.find(f".//{{{NS_CALDAV}}}time-range")
        start, end = _bound(time_range, "start"), _bound(time_range, "end")
        chosen = [(href, item) for href, item in self._members(self.path) if _overlaps(item.data, start, end)]
        self._xml(207, _multistatus("".join(self._data_response(href, item) for href, item in chosen)))


def _bound(element, name):
    text = element.get(name) if element is not None else None
    return _aware(ical.parse_datetime_value(text)) if text else None


def _aware(value):
    moment = value if isinstance(value, datetime) else datetime(value.year, value.month, value.day)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _overlaps(data, start, end):
    try:
        event = ical.parse(data).find("VEVENT")
        first, last = ical.event_span(event) if event is not None else (None, None)
    except ical.IcalError:
        return True
    if first is None:
        return True
    first, last = _aware(first), _aware(last)
    return (start is None or last > start) and (end is None or first < end)


def calendar_server(objects=EXTRA_OBJECTS):
    server = FakeDavServer()
    server.RequestHandlerClass = CalendarDavHandler
    for name, data in objects:
        server.items[f"{WORK}{name}.ics"] = Item(f'"etag-{name}"', data, "text/calendar; charset=utf-8")
    return server.start()


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def server():
    srv = calendar_server()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def lines():
    return []


@pytest.fixture
def store(server, lines):
    uids = iter(["new-1", "new-2", "new-3"])
    return CalDavCalendarStore(client_for(server), START, END, calendar="Work", log=lines.append,
                               uid_factory=lambda: next(uids))


def client_for(server, password=PASSWORD):
    return dav.DavClient(server.base_url, USERNAME, password, timeout=5)


def requests_for(server, verb):
    return [(path, headers) for method, path, headers in server.requests if method == verb]


def stored_event(server, path):
    return ical.parse(server.items[path].data).find("VEVENT")


# -- pure helpers ---------------------------------------------------------------------------
def test_collection_choice_and_paths():
    work = dav.Collection("https://h/cal/work/", "Work", None, ("VEVENT", "VTODO"), "calendar")
    tasks = dav.Collection("https://h/cal/tasks/", "Tasks", None, ("VTODO",), "calendar")
    silent = dav.Collection("https://h/cal/x/", "Unknown", None, (), "calendar")
    assert choose_collection((tasks, work), "") is work and choose_collection((tasks, silent), " ") is silent
    assert choose_collection((work, tasks), "tasks") is tasks and choose_collection((work, tasks), "/cal/tasks") is tasks
    assert choose_collection((work, tasks), "https://h/cal/work/") is work
    with pytest.raises(StoreError) as info:
        choose_collection((work, tasks), "Nope")
    assert str(info.value) == "CalDAV: no calendar called 'Nope' on the server (found: Work, Tasks)"
    with pytest.raises(StoreError, match="no calendar that holds events"):
        choose_collection((tasks,), "")
    with pytest.raises(StoreError, match="found: none"):
        choose_collection((), "Work")
    assert object_path("https://h/cal/x.ics") == "/cal/x.ics" and object_path("/cal/y.ics") == "/cal/y.ics"
    assert object_path("y.ics") == "y.ics"
    assert len(new_uid()) == 36 and new_uid() != new_uid()


def test_read_object_parses_plain_events_and_reports_the_rest():
    record, known = read_object(DENTIST_ICS, '"e"')
    assert record == replace(DENTIST, uid="dentist") and known == KnownObject('"e"', "dentist", False)
    assert read_object(STANDUP_ICS, None)[1] == KnownObject(None, "standup", True)
    for data, problem in ((OVERRIDE_ICS, "holds no event"), (BROKEN_ICS, "no colon on this line"),
                          ("BEGIN:VTODO\nUID:t\nEND:VTODO", "holds no event"), ("", "no iCalendar content")):
        with pytest.raises(UnreadableObject, match=problem):
            read_object(data, None)


# -- discovery ------------------------------------------------------------------------------
def test_discovery_picks_the_calendar_by_name_href_or_first_with_events(server):
    client = client_for(server)
    assert CalDavCalendarStore(client, START, END).collection().display_name == "Work"
    assert CalDavCalendarStore(client, START, END, calendar="tasks").collection().href == server.base_url + "cal/tasks/"
    assert CalDavCalendarStore(client, START, END, calendar="/cal/tasks/").collection().display_name == "Tasks"
    with pytest.raises(StoreError) as info:
        CalDavCalendarStore(client, START, END, calendar="Nope").collection()
    assert "no calendar called 'Nope'" in str(info.value) and "Work, Tasks" in str(info.value)
    bad = CalDavCalendarStore(client_for(server, "wrong-" + PASSWORD), START, END)
    with pytest.raises(StoreError) as info:
        bad.list()
    assert str(info.value).startswith("CalDAV: could not find the calendars: ") and "wrong-" not in str(info.value)
    store = CalDavCalendarStore(client, START, END, calendar="Work")
    server.requests.clear()
    store.list()
    store.list()
    assert [path for verb, path, _ in server.requests if verb == "PROPFIND"] == ["/", "/principals/u/", "/cal/"]   # discovered once


# -- listing --------------------------------------------------------------------------------
def test_list_reports_the_window_and_maps_the_objects(server, store, lines):
    items = store.list()
    assert [(item.id, item.version, item.unreadable) for item in items] == [
        (f"{WORK}a.ics", '"etag-a-1"', False), (f"{WORK}b.ics", '"etag-b-1"', False), (f"{WORK}board.ics", '"etag-board"', False),
        (f"{WORK}broken.ics", '"etag-broken"', True), (f"{WORK}dentist.ics", '"etag-dentist"', False),
        (f"{WORK}nouid.ics", '"etag-nouid"', False), (f"{WORK}override.ics", '"etag-override"', True),
        (f"{WORK}standup.ics", '"etag-standup"', False)]
    by_id = {item.id: item for item in items}
    assert by_id[f"{WORK}dentist.ics"].record == replace(DENTIST, uid="dentist") and not by_id[f"{WORK}dentist.ics"].read_only
    assert by_id[f"{WORK}board.ics"].record == Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 12), all_day=True,
                                                         busy_status="free", uid="board")
    assert by_id[f"{WORK}standup.ics"].read_only and by_id[f"{WORK}nouid.ics"].record.uid == ""
    assert by_id[f"{WORK}a.ics"].record.start == datetime(2026, 3, 10, 10) and not by_id[f"{WORK}a.ics"].read_only
    assert "no colon on this line" in by_id[f"{WORK}broken.ics"].problem
    assert by_id[f"{WORK}override.ics"].problem == "it holds no event (or only recurrence overrides)"
    broken, override = sorted(lines)
    assert broken.startswith(f"{WORK}broken.ics cannot be read (") and broken.endswith("); it is left alone")
    assert override == f"{WORK}override.ics cannot be read (it holds no event (or only recurrence overrides)); it is left alone"
    (path, headers), = requests_for(server, "REPORT")
    assert path == WORK and headers.get("Depth") == "1" and store.name == "caldav"


# -- writing --------------------------------------------------------------------------------
def test_create_puts_a_new_object_conditionally(server, store):
    path = store.create(DENTIST)
    assert path == f"{WORK}new-1.ics" and path in server.items
    request_path, headers = requests_for(server, "PUT")[-1]
    assert request_path == path and headers.get("If-None-Match") == "*" and "If-Match" not in headers
    assert headers.get("Content-Type") == CONTENT_TYPE
    event = stored_event(server, path)
    assert from_vevent(event) == replace(DENTIST, uid="new-1") and event.value("DTSTART") == "20260907T073000Z"
    (created,) = [item for item in store.list() if item.id == path]
    assert created.version == server.items[path].etag
    board = store.create(Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 12), all_day=True))
    assert stored_event(server, board).value("DTEND") == "20260912"
    assert store.update(board, Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 11), all_day=True)) == server.items[board].etag
    assert requests_for(server, "PUT")[-1][1].get("If-Match") == '"etag-3"'      # the etag the create returned


def test_update_is_conditional_on_the_listed_etag(server, store):
    store.list()
    etag = store.update(f"{WORK}dentist.ics", replace(DENTIST, location="Room 2", categories=()))
    request_path, headers = requests_for(server, "PUT")[-1]
    assert request_path == f"{WORK}dentist.ics" and headers.get("If-Match") == '"etag-dentist"'
    assert etag == server.items[request_path].etag and etag != '"etag-dentist"'
    event = stored_event(server, request_path)
    assert (event.value("UID"), event.value("LOCATION"), event.get("CATEGORIES")) == ("dentist", "Room 2", None)
    assert store.update(f"{WORK}nouid.ics", DENTIST) and stored_event(server, f"{WORK}nouid.ics").value("UID") == "new-1"
    fresh = CalDavCalendarStore(client_for(server), START, END, calendar="Work")
    moved = Appointment("March meeting (moved)", datetime(2026, 3, 11, 10), datetime(2026, 3, 11, 11))
    assert fresh.update(f"{WORK}a.ics", moved) == server.items[f"{WORK}a.ics"].etag
    verbs = [(verb, path) for verb, path, _ in server.requests][-2:]
    assert verbs == [("GET", f"{WORK}a.ics"), ("PUT", f"{WORK}a.ics")]                  # never listed: fetched first
    assert requests_for(server, "PUT")[-1][1].get("If-Match") == '"etag-a-1"'
    assert from_vevent(stored_event(server, f"{WORK}a.ics")) == replace(moved, uid="a")


def test_delete_uses_the_listed_etag_and_tolerates_gone_objects(server, store, lines):
    store.list()
    store.delete(f"{WORK}b.ics")
    request_path, headers = requests_for(server, "DELETE")[-1]
    assert request_path == f"{WORK}b.ics" and headers.get("If-Match") == '"etag-b-1"' and request_path not in server.items
    store.delete(f"{WORK}b.ics")
    assert lines[-1] == f"{WORK}b.ics was already gone from the server"
    fresh = CalDavCalendarStore(client_for(server), START, END, calendar="Work")
    fresh.delete(f"{WORK}old.ics")
    assert "If-Match" not in requests_for(server, "DELETE")[-1][1] and f"{WORK}old.ics" not in server.items


# -- failures -------------------------------------------------------------------------------
def test_conflicts_and_missing_objects_become_store_errors(server, store):
    store.list()
    server.items[f"{WORK}dentist.ics"] = Item('"etag-dentist-2"', DENTIST_ICS, "text/calendar")
    with pytest.raises(StoreError) as info:
        store.update(f"{WORK}dentist.ics", DENTIST)
    assert "changed on the server since it was listed; run the sync again" in str(info.value)
    with pytest.raises(StoreError, match="could not delete the event: it changed on the server"):
        store.delete(f"{WORK}dentist.ics")
    with pytest.raises(StoreError, match="recurring series on the server"):
        store.update(f"{WORK}standup.ics", DENTIST)
    fresh = CalDavCalendarStore(client_for(server), START, END, calendar="Work")
    with pytest.raises(StoreError, match="no longer on the server"):
        fresh.update(f"{WORK}missing.ics", DENTIST)
    with pytest.raises(StoreError, match="not a plain event"):
        fresh.update(f"{WORK}broken.ics", DENTIST)
    with pytest.raises(StoreError, match="not a plain event"):
        fresh.update(f"{WORK}override.ics", DENTIST)
    for bad in ("", "  ", f"{WORK}a b.ics"):
        with pytest.raises(StoreError, match="not a calendar object path"):
            store.update(bad, DENTIST)
        with pytest.raises(StoreError, match="not a calendar object path"):
            store.delete(bad)
    unauthorized = CalDavCalendarStore(client_for(server, "wrong"), START, END, calendar="Work")
    unauthorized._collection = store.collection()
    with pytest.raises(StoreError) as info:
        unauthorized.delete(f"{WORK}a.ics")
    assert str(info.value).startswith("CalDAV: could not delete the event: ") and "wrong" not in str(info.value)
    with pytest.raises(StoreError, match="could not create the event 'Dentist'"):
        unauthorized.create(DENTIST)


# -- backend spec ---------------------------------------------------------------------------
def test_build_reads_settings_and_secrets(server, tmp_path):
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None, extra={"now": datetime(2026, 3, 1)})
    account = Account("c", "calendar", "caldav", (("url", server.base_url), ("username", USERNAME), ("calendar", "Work")))
    store = caldav.build(account, {"password": PASSWORD}, context)
    assert isinstance(store, WindowedStore) and isinstance(store.inner, CalDavCalendarStore)
    assert store.window == (START, END) and store.skip_recurring
    assert f"{WORK}standup.ics" not in [item.id for item in store.list()] and f"{WORK}a.ics" in [i.id for i in store.list()]
    shown = caldav.build(account.with_setting("recurring", "first"), {"password": PASSWORD}, context)
    assert f"{WORK}standup.ics" in [item.id for item in shown.list()]
    for incomplete, secrets in ((Account("c", "calendar", "caldav"), {"password": "p"}),
                                (Account("c", "calendar", "caldav", (("url", "https://h/"),)), {"password": "p"}),
                                (account, {}), (account, {"password": ""})):
        with pytest.raises(AccountError, match="needs url, username and password"):
            caldav.build(incomplete, secrets, context)
    with pytest.raises(AccountError, match="base URL must be http"):
        caldav.build(Account("c", "calendar", "caldav", (("url", "not a url"), ("username", "u"))), {"password": "p"}, context)
    seen = []

    def transport(request):
        seen.append(request)
        return dav.Response(500, (), b"boom")

    hooked = caldav.build(account, {"password": PASSWORD}, BuildContext(log=lambda _l: None, sync_dir=tmp_path,
                                                                         save_secrets=lambda _c: None, extra={TRANSPORT_KEY: transport}))
    with pytest.raises(StoreError, match="could not find the calendars"):
        hooked.list()
    assert seen and seen[0].method == "PROPFIND"
    assert caldav.BACKEND.key == "caldav" and caldav.BACKEND.login is None and caldav.BACKEND.notes
    assert [(s.key, s.required, s.secret) for s in caldav.BACKEND.settings] == [
        ("url", True, False), ("username", True, False), ("password", True, True), ("calendar", False, False)]
    assert caldav.BACKEND.missing_settings(Account("c", "calendar", "caldav"), {}) == ("url", "username", "password")
    assert caldav.BACKEND.missing_settings(account, {"password": "x"}) == ()
