"""The Microsoft 365 backend against a fake transport: Graph paths, the window filter, paging, named
calendars, body mapping in both directions (UTC times, all-day, reminders), errors and the OAuth token."""
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Appointment
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.calendar import m365
from jornada.sync.calendar.m365 import (GRAPH_BASE, SELECT_FIELDS, GraphCalendarStore, appointment_to_event, body_text,
                                        event_span, event_to_appointment, graph_moment, reminder_of, utc_text)
from jornada.sync.calendar.window import WindowedStore
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpClient, HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

EVENTS = "/v1.0/me/calendar/events"
CALENDARS = "/v1.0/me/calendars"
START, END = datetime(2026, 1, 30), datetime(2027, 3, 2)
UTC = timezone.utc
DENTIST = Appointment("Dentist", datetime(2026, 9, 7, 9, 30), datetime(2026, 9, 7, 10, 15), location="Clinic",
                      notes="bring card\nsecond", categories=("Health",), busy_status="tentative", private=True,
                      reminder_minutes=15)


def utc(text):
    return {"dateTime": text, "timeZone": "UTC"}


DENTIST_EVENT = {
    "id": "M1", "changeKey": "ck1", "subject": "Dentist",
    "body": {"contentType": "html", "content": "<html><head><style>p{}</style></head><body><p>bring card</p><p>second</p></body></html>"},
    "start": utc("2026-09-07T07:30:00.0000000"), "end": utc("2026-09-07T08:15:00.0000000"), "isAllDay": False,
    "location": {"displayName": "Clinic"}, "showAs": "tentative", "sensitivity": "private", "isReminderOn": True,
    "reminderMinutesBeforeStart": 15, "categories": ["Health", " "], "type": "singleInstance", "iCalUId": "uid-m1",
}
BOARD_EVENT = {"id": "M2", "subject": "Board", "body": {"contentType": "text", "content": ""}, "isAllDay": True,
               "start": utc("2026-09-09T22:00:00.0000000"), "end": utc("2026-09-11T22:00:00.0000000"), "showAs": "free",
               "isReminderOn": False}
STANDUP_EVENT = {"id": "M3", "changeKey": "ck3", "subject": "Standup", "start": utc("2026-09-08T07:00:00.0000000"),
                 "end": utc("2026-09-08T07:15:00.0000000"), "type": "seriesMaster",
                 "recurrence": {"pattern": {"type": "weekly"}}, "showAs": "oof"}
BROKEN_EVENT = {"id": "M4", "subject": "no start"}


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def paged(first, second, marker):
    def handler(request):
        if marker in urlsplit(request.url).query:
            return json_response(200, {"value": second})
        path = urlsplit(request.url).path[len("/v1.0"):]
        return json_response(200, {"value": first, "@odata.nextLink": f"{GRAPH_BASE}{path}?$skiptoken={marker}"})
    return handler


def routes(extra=None):
    base = {
        ("GET", EVENTS): paged([DENTIST_EVENT, BOARD_EVENT], [STANDUP_EVENT, BROKEN_EVENT, "junk", {}], "page2"),
        ("GET", CALENDARS): paged([{"id": "C1", "name": "Calendar"}], [{"id": "C2", "name": "Handheld"}, {"name": "No id"}], "cal2"),
        ("POST", EVENTS): json_response(201, {"id": "M9", "changeKey": "ck9"}),
        ("PATCH", "/v1.0/me/events/M1"): json_response(200, {"id": "M1", "changeKey": "ck2"}),
        ("DELETE", "/v1.0/me/events/M1"): HttpResponse(204),
        ("DELETE", "/v1.0/me/events/M2"): HttpResponse(404),
    }
    return {**base, **(extra or {})}


def store_with(extra=None, calendar_name="", lines=None, zone_name=lambda: "Europe/Berlin"):
    transport, seen = fake_transport(routes(extra))
    log = lines.append if lines is not None else (lambda _line: None)
    http = HttpClient(GRAPH_BASE, transport=transport, retries=0)
    return GraphCalendarStore(http, START, END, calendar_name=calendar_name, log=log, zone_name=zone_name), seen


# -- mapping (pure) ----------------------------------------------------------------------
def test_graph_moments_are_read_in_their_zone_and_trimmed_to_microseconds():
    assert graph_moment(utc("2026-09-07T07:30:00.1234567")) == datetime(2026, 9, 7, 7, 30, 0, 123456, tzinfo=UTC)
    assert graph_moment({"dateTime": "2026-09-07T09:30:00.0000000", "timeZone": "Europe/Berlin"}).utcoffset() == timedelta(hours=2)
    floating = graph_moment({"dateTime": "2026-09-07T09:30:00", "timeZone": "Pacific Standard Time"})
    assert floating.tzinfo is None and floating == datetime(2026, 9, 7, 9, 30)
    assert graph_moment({"dateTime": "2026-09-07T09:30:00Z"}).tzinfo is UTC
    assert graph_moment({"dateTime": "2026-09-07"}) == datetime(2026, 9, 7, tzinfo=UTC)
    for bad in (None, "2026-09-07", {"dateTime": 5}, {}, {"dateTime": "not a date"}):
        with pytest.raises(ValueError):
            graph_moment(bad)
    assert utc_text(datetime(2026, 9, 7, 9, 30)) == "2026-09-07T07:30:00"
    assert utc_text(datetime(2026, 1, 15, 9)) == "2026-01-15T08:00:00"


def test_event_span_snaps_all_day_events_to_local_midnights():
    assert event_span(BOARD_EVENT) == (datetime(2026, 9, 10), datetime(2026, 9, 12), True)
    elsewhere = {"start": utc("2026-09-10T07:00:00"), "end": utc("2026-09-11T07:00:00"), "isAllDay": True}
    assert event_span(elsewhere) == (datetime(2026, 9, 10), datetime(2026, 9, 11), True)   # created in another zone
    assert event_span({"start": utc("2026-09-09T22:00:00"), "isAllDay": True}) == (datetime(2026, 9, 10), datetime(2026, 9, 11), True)
    backwards = {"start": utc("2026-09-07T08:00:00"), "end": utc("2026-09-07T07:00:00")}
    assert event_span(backwards) == (datetime(2026, 9, 7, 10), datetime(2026, 9, 7, 10), False)
    with pytest.raises(ValueError):
        event_span(BROKEN_EVENT)


def test_event_mapping_flattens_html_bodies_and_reads_every_field():
    assert event_to_appointment(DENTIST_EVENT) == replace(DENTIST, uid="uid-m1")
    assert event_to_appointment(BOARD_EVENT) == Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 12),
                                                            all_day=True, busy_status="free", uid="M2")
    standup = event_to_appointment(STANDUP_EVENT)
    assert standup.recurring and standup.busy_status == "out_of_office" and standup.uid == "M3"
    assert event_to_appointment({**STANDUP_EVENT, "type": "occurrence", "recurrence": None}).recurring
    assert not event_to_appointment({**STANDUP_EVENT, "type": "singleInstance", "recurrence": None}).recurring
    quirky = event_to_appointment({**DENTIST_EVENT, "showAs": "workingElsewhere", "sensitivity": "confidential",
                                   "reminderMinutesBeforeStart": "x", "location": "Clinic", "categories": None})
    assert quirky.busy_status == "busy" and quirky.private and quirky.reminder_minutes is None
    assert quirky.location == "" and quirky.categories == ()
    plain = event_to_appointment({**DENTIST_EVENT, "showAs": "unknown", "sensitivity": "normal", "isReminderOn": False})
    assert plain.busy_status == "busy" and not plain.private and plain.reminder_minutes is None
    assert body_text({"contentType": "text", "content": "a\r\nb"}) == "a\r\nb" and body_text(None) == ""
    assert body_text({"contentType": "HTML", "content": "<p>x</p>"}) == "x" and body_text({"content": 5}) == ""
    assert reminder_of({"isReminderOn": True}) == 0 and reminder_of({"isReminderOn": True, "reminderMinutesBeforeStart": -5}) == 0


def test_appointment_to_event_sends_utc_times_and_zone_midnights_for_all_day():
    assert appointment_to_event(replace(DENTIST, notes="bring card")) == {
        "subject": "Dentist", "body": {"contentType": "text", "content": "bring card"},
        "start": utc("2026-09-07T07:30:00"), "end": utc("2026-09-07T08:15:00"), "isAllDay": False,
        "location": {"displayName": "Clinic"}, "showAs": "tentative", "sensitivity": "private",
        "isReminderOn": True, "categories": ["Health"], "reminderMinutesBeforeStart": 15,
    }
    board = Appointment("Board", datetime(2026, 9, 10, 8), datetime(2026, 9, 11, 17), all_day=True, busy_status="out_of_office")
    body = appointment_to_event(board, "Europe/Berlin")
    assert body["start"] == {"dateTime": "2026-09-10T00:00:00", "timeZone": "Europe/Berlin"}
    assert body["end"] == {"dateTime": "2026-09-12T00:00:00", "timeZone": "Europe/Berlin"}
    assert body["isAllDay"] and body["showAs"] == "oof" and body["sensitivity"] == "normal"
    assert not body["isReminderOn"] and "reminderMinutesBeforeStart" not in body and body["categories"] == []
    assert appointment_to_event(board)["start"]["timeZone"] == "UTC"
    clamped = appointment_to_event(replace(DENTIST, end=datetime(2026, 9, 7, 8), busy_status="weird", reminder_minutes=-3))
    assert clamped["end"] == clamped["start"] and clamped["showAs"] == "busy" and clamped["reminderMinutesBeforeStart"] == 0


# -- the store ------------------------------------------------------------------------------
def test_list_filters_on_the_window_in_utc_and_follows_paging():
    lines = []
    store, seen = store_with(lines=lines)
    dentist, board, standup, broken = store.list()
    assert [(r.method, urlsplit(r.url).path) for r in seen] == [("GET", EVENTS)] * 2
    query = parse_qs(urlsplit(seen[0].url).query)
    assert query["$filter"] == ["start/dateTime ge '2026-01-29T23:00:00' and start/dateTime le '2027-03-01T23:00:00'"]
    assert query["$top"] == ["100"] and query["$select"] == [SELECT_FIELDS]
    assert all(r.header("Prefer") == 'outlook.timezone="UTC"' for r in seen)
    assert "skiptoken=page2" in seen[1].url and "$filter" not in seen[1].url
    assert (dentist.id, dentist.version) == ("M1", "ck1") and dentist.record == replace(DENTIST, uid="uid-m1")
    assert board.version is None and board.record.all_day
    assert (standup.id, standup.version) == ("M3", "ck3") and standup.record.recurring and standup.read_only
    assert not dentist.read_only and (broken.id, broken.unreadable, broken.problem) == ("M4", True, "event time is missing")
    assert lines == ["Microsoft 365 event M4 cannot be read (event time is missing); it is left alone"]
    assert store.name == "m365-calendar" and store.events_path() == "/me/calendar/events"


def test_events_beyond_the_datetime_range_are_skipped_not_fatal():
    lines = []
    doomsday = {"id": "M6", "subject": "End of days", "isAllDay": True, "start": utc("9999-12-31T22:00:00"),
                "end": utc("9999-12-31T23:00:00")}
    store, _ = store_with({("GET", EVENTS): json_response(200, {"value": [doomsday, DENTIST_EVENT]})}, lines=lines)
    assert [(item.id, item.unreadable) for item in store.list()] == [("M6", True), ("M1", False)]
    assert lines == ["Microsoft 365 event M6 cannot be read (date value out of range); it is left alone"]


def test_named_calendar_is_resolved_by_display_name():
    store, seen = store_with({("GET", f"{CALENDARS}/C2/events"): json_response(200, {"value": [DENTIST_EVENT]})}, "handheld")
    assert [item.id for item in store.list()] == ["M1"]
    assert [urlsplit(r.url).path for r in seen] == [CALENDARS, CALENDARS, f"{CALENDARS}/C2/events"]
    assert parse_qs(urlsplit(seen[0].url).query) == {"$select": ["id,name"], "$top": ["100"]}
    assert store.events_path() == "/me/calendars/C2/events" and len(seen) == 3       # looked up once
    missing, _ = store_with(calendar_name="Nope")
    with pytest.raises(StoreError) as info:
        missing.list()
    assert "no calendar called 'Nope' (found: Calendar, Handheld, No id)" in str(info.value)
    forbidden, _ = store_with({("GET", CALENDARS): json_response(403, {"error": "no"})}, "Handheld")
    with pytest.raises(StoreError, match="could not list the calendars: HTTP 403"):
        forbidden.list()


def test_create_update_delete_send_graph_payloads():
    lines = []
    store, seen = store_with(lines=lines)
    board = Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 12), all_day=True)
    assert store.create(board) == "M9"
    assert (seen[-1].method, urlsplit(seen[-1].url).path) == ("POST", EVENTS)
    assert json.loads(seen[-1].body) == appointment_to_event(board, "Europe/Berlin")
    assert seen[-1].header("Content-Type") == "application/json"
    assert store.update("M1", DENTIST) == "ck2"
    assert (seen[-1].method, urlsplit(seen[-1].url).path) == ("PATCH", "/v1.0/me/events/M1")
    assert json.loads(seen[-1].body) == appointment_to_event(DENTIST)
    store.delete("M1")
    assert (seen[-1].method, urlsplit(seen[-1].url).path) == ("DELETE", "/v1.0/me/events/M1")
    store.delete("M2")
    assert lines == ["Microsoft 365 event M2 was already gone"]
    with pytest.raises(StoreError, match="needs an event id"):
        store.update(" ", DENTIST)
    with pytest.raises(StoreError, match="needs an event id"):
        store.delete("")


def test_a_listed_series_is_never_rewritten():
    store, seen = store_with()
    store.list()
    with pytest.raises(StoreError) as info:
        store.update("M3", DENTIST)
    assert "recurring series in Microsoft 365" in str(info.value) and len(seen) == 2
    assert store.update("M1", DENTIST) == "ck2"


def test_failures_become_store_errors(monkeypatch):
    forbidden, _ = store_with({("GET", EVENTS): json_response(403, {"error": {"message": "Forbidden"}})})
    with pytest.raises(StoreError) as info:
        forbidden.list()
    assert str(info.value).startswith("Microsoft 365: could not list the events: HTTP 403")
    broken, _ = store_with({("GET", EVENTS): HttpResponse(200, (), b"not json")})
    with pytest.raises(StoreError):
        broken.list()
    odd, _ = store_with({("GET", EVENTS): json_response(200, [1])})
    with pytest.raises(StoreError, match="unexpected reply"):
        odd.list()
    no_id, _ = store_with({("POST", EVENTS): json_response(201, {"nope": 1})})
    with pytest.raises(StoreError, match="did not report the id"):
        no_id.create(DENTIST)
    failing_delete, _ = store_with({("DELETE", "/v1.0/me/events/M1"): HttpResponse(500, (), b"boom")})
    with pytest.raises(StoreError, match="could not delete the event: HTTP 500"):
        failing_delete.delete("M1")
    endless, _ = store_with({("GET", EVENTS): json_response(200, {"value": [], "@odata.nextLink": f"{GRAPH_BASE}/me/calendar/events?x=1"})})
    monkeypatch.setattr(m365, "MAX_PAGES", 2)
    with pytest.raises(StoreError, match="did not end after 2 pages"):
        endless.list()


# -- backend spec ------------------------------------------------------------------------------
def test_build_uses_the_oauth_token_tenant_and_calendar_settings(tmp_path):
    transport, seen = fake_transport(routes())
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           http_transport=transport, extra={"now": datetime(2026, 3, 1)})
    secrets = {TOKEN_KEY: Token("tok", "rt", 4_000_000_000.0).to_dict()}
    account = Account("m", "calendar", "m365", (("client_id", "cid"), ("tenant", "contoso.com")))
    store = m365.build(account, secrets, context)
    assert isinstance(store, WindowedStore) and isinstance(store.inner, GraphCalendarStore)
    assert store.window == (START, END) and store.skip_recurring
    listed = store.list()
    assert [(item.id, item.unreadable) for item in listed] == [("M1", False), ("M2", False), ("M4", True)]   # no series
    assert seen[0].header("Authorization") == "Bearer tok" and urlsplit(seen[0].url).path == EVENTS
    named = m365.build(account.with_setting("calendar", "Handheld"), secrets, context)
    assert named.inner.events_path() == "/me/calendars/C2/events"
    assert m365.tenant_of(account) == "contoso.com" and m365.tenant_of(Account("m", "calendar", "m365")) == "common"
    with pytest.raises(AccountError, match="tenant"):
        m365.tenant_of(Account("m", "calendar", "m365", (("tenant", "bad tenant!"),)))
    with pytest.raises(AccountError):
        m365.login(Account("m", "calendar", "m365"), {}, context)
    assert m365.BACKEND.key == "m365" and m365.BACKEND.login is not None and m365.BACKEND.notes
    assert [s.key for s in m365.BACKEND.settings] == ["client_id", "client_secret", "token", "tenant", "calendar"]
    assert m365.BACKEND.missing_settings(Account("m", "calendar", "m365"), {}) == ("client_id", "token")
    assert m365.BACKEND.missing_settings(account, secrets) == ()
