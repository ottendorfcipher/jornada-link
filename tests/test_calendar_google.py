"""The Google Calendar backend against a fake transport: paths, query parameters, paging, cancelled
events, body mapping in both directions, error handling and the OAuth token."""
import json
import time
from dataclasses import replace
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Appointment
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.calendar import google
from jornada.sync.calendar.google import (API_BASE, BUSY_KEY, CATEGORIES_KEY, GoogleCalendarStore, appointment_to_event,
                                          busy_status_of, event_span, event_to_appointment, reminder_of)
from jornada.sync.calendar.window import WindowedStore
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpClient, HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

EVENTS = "/calendar/v3/calendars/primary/events"
START, END = datetime(2026, 1, 30), datetime(2027, 3, 2)
DENTIST = Appointment("Dentist", datetime(2026, 9, 7, 9, 30), datetime(2026, 9, 7, 10, 15), location="Clinic",
                      notes="bring card", categories=("Health", "A"), busy_status="tentative", private=True,
                      reminder_minutes=15)
DENTIST_EVENT = {
    "id": "E1", "etag": '"e1"', "status": "confirmed", "summary": "Dentist", "location": "Clinic",
    "description": "bring card", "start": {"dateTime": "2026-09-07T09:30:00+02:00"},
    "end": {"dateTime": "2026-09-07T10:15:00+02:00"}, "transparency": "opaque", "visibility": "private",
    "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 15}]},
    "extendedProperties": {"private": {CATEGORIES_KEY: "Health, A", BUSY_KEY: "tentative"}}, "iCalUID": "e1@google.com",
}
BOARD_EVENT = {"id": "E2", "summary": "Board", "start": {"date": "2026-09-10"}, "end": {"date": "2026-09-12"},
               "transparency": "transparent", "reminders": {"useDefault": True}}
CANCELLED_EVENT = {"id": "E3", "status": "cancelled"}
STANDUP_EVENT = {"id": "E4", "etag": '"e4"', "summary": "Standup", "start": {"dateTime": "2026-09-08T07:00:00Z"},
                 "end": {"dateTime": "2026-09-08T07:15:00Z"}, "recurrence": ["RRULE:FREQ=WEEKLY"]}
BROKEN_EVENT = {"id": "E5", "summary": "no start"}


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def paged(first, second, token):
    def handler(request):
        query = parse_qs(urlsplit(request.url).query)
        if query.get("pageToken") == [token]:
            return json_response(200, {"items": second})
        return json_response(200, {"items": first, "nextPageToken": token})
    return handler


def routes(extra=None):
    base = {
        ("GET", EVENTS): paged([DENTIST_EVENT, BOARD_EVENT, CANCELLED_EVENT], [STANDUP_EVENT, BROKEN_EVENT, "junk", {}], "page2"),
        ("POST", EVENTS): json_response(200, {"id": "E9", "etag": '"e9"'}),
        ("PUT", f"{EVENTS}/E1"): json_response(200, {"id": "E1", "etag": '"e1b"'}),
        ("DELETE", f"{EVENTS}/E1"): HttpResponse(204),
        ("DELETE", f"{EVENTS}/E2"): HttpResponse(410),
    }
    return {**base, **(extra or {})}


def store_with(extra=None, calendar_id="primary", lines=None):
    transport, seen = fake_transport(routes(extra))
    log = lines.append if lines is not None else (lambda _line: None)
    http = HttpClient(API_BASE, transport=transport, retries=0)
    return GoogleCalendarStore(http, START, END, calendar_id=calendar_id, log=log), seen


# -- mapping (pure) ----------------------------------------------------------------------
def test_event_mapping_reads_every_field():
    assert event_to_appointment(DENTIST_EVENT) == replace(DENTIST, uid="e1@google.com")
    assert event_to_appointment(BOARD_EVENT) == Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 12),
                                                            all_day=True, busy_status="free", uid="E2")
    standup = event_to_appointment(STANDUP_EVENT)
    assert standup.recurring and standup.start == datetime(2026, 9, 8, 9) and standup.uid == "E4"
    instance = event_to_appointment({"id": "x_2026", "recurringEventId": "x", "start": {"dateTime": "2026-09-08T07:00:00Z"}})
    assert instance.recurring and instance.uid == "x_2026" and instance.busy_status == "busy" and instance.end == instance.start
    round_trip = event_to_appointment({**appointment_to_event(DENTIST), "id": "new"})
    assert round_trip == replace(DENTIST, uid="new")


def test_event_span_and_reminder_edge_cases():
    backwards = event_span({"dateTime": "2026-09-07T10:00:00+02:00"}, {"dateTime": "2026-09-07T09:00:00+02:00"})
    assert backwards == (datetime(2026, 9, 7, 10), datetime(2026, 9, 7, 10), False)
    assert event_span({"date": "2026-09-10"}, None) == (datetime(2026, 9, 10), datetime(2026, 9, 11), True)
    assert event_span({"date": "2026-09-10"}, {"date": "2026-09-01"}) == (datetime(2026, 9, 10), datetime(2026, 9, 11), True)
    for start, end in ((None, None), ({}, None), ({"dateTime": " "}, None), ({"dateTime": "2026-13-01T00:00:00Z"}, None)):
        with pytest.raises(ValueError):
            event_span(start, end)
    assert reminder_of({"useDefault": True, "overrides": [{"method": "popup", "minutes": 5}]}) is None
    assert reminder_of({"useDefault": False, "overrides": [{"method": "email", "minutes": 5}]}) is None
    assert reminder_of({"useDefault": False, "overrides": [{"method": "popup", "minutes": "x"}, {"method": "popup", "minutes": -3}]}) == 0
    assert reminder_of({"useDefault": False, "overrides": [{"method": "popup"}]}) == 0
    assert reminder_of(None) is None and reminder_of({"useDefault": False}) is None
    assert busy_status_of({"transparency": "transparent"}, {BUSY_KEY: "busy"}) == "free"
    assert busy_status_of({}, {BUSY_KEY: "out_of_office"}) == "out_of_office" and busy_status_of({}, {BUSY_KEY: "weird"}) == "busy"


def test_appointment_to_event_body_in_both_shapes():
    messy = replace(DENTIST, summary=" Dentist ", start=datetime(2026, 9, 7, 9, 30, 0, 500), notes="bring card\r\n",
                    categories=("Health", " A ", "Health"))
    assert appointment_to_event(messy) == {
        "summary": "Dentist", "location": "Clinic", "description": "bring card",
        "start": {"dateTime": "2026-09-07T09:30:00+02:00"}, "end": {"dateTime": "2026-09-07T10:15:00+02:00"},
        "transparency": "opaque", "visibility": "private",
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
        "extendedProperties": {"private": {CATEGORIES_KEY: "Health,A", BUSY_KEY: "tentative"}},
    }
    board = appointment_to_event(Appointment("Board", datetime(2026, 9, 10, 8), datetime(2026, 9, 11, 17), all_day=True,
                                             busy_status="free"))
    assert (board["start"], board["end"]) == ({"date": "2026-09-10"}, {"date": "2026-09-12"})
    assert board["transparency"] == "transparent" and board["visibility"] == "default"
    assert board["reminders"] == {"useDefault": True}
    assert board["extendedProperties"]["private"] == {CATEGORIES_KEY: "", BUSY_KEY: "free"}
    long = appointment_to_event(replace(DENTIST, end=datetime(2026, 9, 7, 8), reminder_minutes=99_999))
    assert long["reminders"]["overrides"] == [{"method": "popup", "minutes": 40_320}] and long["end"] == long["start"]
    assert appointment_to_event(replace(DENTIST, reminder_minutes=-1))["reminders"]["overrides"][0]["minutes"] == 0
    assert appointment_to_event(replace(DENTIST, busy_status="weird"))["extendedProperties"]["private"][BUSY_KEY] == "busy"


# -- the store ------------------------------------------------------------------------------
def test_list_pages_through_the_window_and_skips_cancelled_and_broken_events():
    lines = []
    store, seen = store_with(lines=lines)
    dentist, board, standup, broken = store.list()
    assert [(r.method, urlsplit(r.url).path) for r in seen] == [("GET", EVENTS)] * 2
    assert parse_qs(urlsplit(seen[0].url).query) == {
        "timeMin": ["2026-01-30T00:00:00+01:00"], "timeMax": ["2027-03-02T00:00:00+01:00"],
        "singleEvents": ["false"], "showDeleted": ["false"], "maxResults": ["250"]}
    assert parse_qs(urlsplit(seen[1].url).query)["pageToken"] == ["page2"]
    assert (dentist.id, dentist.version) == ("E1", '"e1"') and board.version is None
    assert dentist.record == replace(DENTIST, uid="e1@google.com")
    assert board.record == Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 12), all_day=True,
                                       busy_status="free", uid="E2")
    assert standup.record.recurring and standup.read_only and (standup.id, standup.version) == ("E4", '"e4"')
    assert not dentist.read_only and (broken.id, broken.unreadable, broken.problem) == ("E5", True, "event has no start")
    assert lines == ["Google Calendar event E5 cannot be read (event has no start); it is left alone"]
    assert store.name == "google-calendar" and store.calendar_id == "primary"
    assert store_with(calendar_id=" ")[0].calendar_id == "primary"


def test_events_beyond_the_datetime_range_are_skipped_not_fatal():
    lines = []
    doomsday = {"id": "E6", "summary": "End of days", "start": {"date": "9999-12-31"}}
    store, _ = store_with({("GET", EVENTS): json_response(200, {"items": [doomsday, DENTIST_EVENT]})}, lines=lines)
    assert [(item.id, item.unreadable) for item in store.list()] == [("E6", True), ("E1", False)]
    assert lines == ["Google Calendar event E6 cannot be read (date value out of range); it is left alone"]


def test_create_update_delete_send_the_event_resource():
    lines = []
    store, seen = store_with(lines=lines)
    assert store.create(DENTIST) == "E9"
    assert (seen[-1].method, urlsplit(seen[-1].url).path) == ("POST", EVENTS)
    assert json.loads(seen[-1].body) == appointment_to_event(DENTIST)
    assert seen[-1].header("Content-Type") == "application/json"
    assert store.update("E1", replace(DENTIST, location="Room 2")) == '"e1b"'
    assert (seen[-1].method, urlsplit(seen[-1].url).path) == ("PUT", f"{EVENTS}/E1")
    assert json.loads(seen[-1].body)["location"] == "Room 2"
    store.delete("E1")
    assert (seen[-1].method, urlsplit(seen[-1].url).path) == ("DELETE", f"{EVENTS}/E1")
    store.delete("E2")
    assert lines == ["Google Calendar event E2 was already gone"]
    with pytest.raises(StoreError, match="needs an event id"):
        store.update("", DENTIST)
    with pytest.raises(StoreError, match="needs an event id"):
        store.delete(" ")


def test_a_listed_series_is_never_rewritten():
    store, seen = store_with()
    store.list()
    with pytest.raises(StoreError) as info:
        store.update("E4", DENTIST)
    assert "recurring series in Google Calendar" in str(info.value) and len(seen) == 2
    assert store.update("E1", DENTIST) == '"e1b"'


def test_failures_become_store_errors(monkeypatch):
    forbidden, _ = store_with({("GET", EVENTS): json_response(403, {"error": {"message": "insufficient scope"}})})
    with pytest.raises(StoreError) as info:
        forbidden.list()
    assert str(info.value).startswith("Google Calendar: could not list the events: HTTP 403")
    odd, _ = store_with({("GET", EVENTS): json_response(200, [1, 2])})
    with pytest.raises(StoreError, match="unexpected reply"):
        odd.list()
    no_id, _ = store_with({("POST", EVENTS): json_response(200, {"kind": "calendar#event"})})
    with pytest.raises(StoreError, match="did not report the id"):
        no_id.create(DENTIST)
    text_reply, _ = store_with({("PUT", f"{EVENTS}/E1"): HttpResponse(200, (), b"not json")})
    with pytest.raises(StoreError, match="could not update the event 'Dentist'"):
        text_reply.update("E1", DENTIST)
    failing_delete, _ = store_with({("DELETE", f"{EVENTS}/E1"): json_response(403, {"error": "no"})})
    with pytest.raises(StoreError, match="could not delete the event: HTTP 403"):
        failing_delete.delete("E1")
    endless, _ = store_with({("GET", EVENTS): json_response(200, {"items": [], "nextPageToken": "again"})})
    monkeypatch.setattr(google, "MAX_PAGES", 3)
    with pytest.raises(StoreError, match="did not end after 3 pages"):
        endless.list()


# -- backend spec ------------------------------------------------------------------------------
def test_build_uses_the_oauth_token_and_the_calendar_setting(tmp_path):
    shared = "/calendar/v3/calendars/abc%40group.calendar.google.com/events"
    transport, seen = fake_transport(routes({("GET", shared): json_response(200, {"items": [DENTIST_EVENT, STANDUP_EVENT]})}))
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           http_transport=transport, extra={"now": datetime(2026, 3, 1)})
    secrets = {TOKEN_KEY: Token("tok", "rt", 4_000_000_000.0).to_dict()}
    account = Account("g", "calendar", "google", (("client_id", "cid"), ("calendar_id", "abc@group.calendar.google.com")))
    store = google.build(account, secrets, context)
    assert isinstance(store, WindowedStore) and isinstance(store.inner, GoogleCalendarStore)
    assert store.window == (START, END) and store.skip_recurring
    assert [item.id for item in store.list()] == ["E1"]
    assert urlsplit(seen[0].url).path == shared and seen[0].header("Authorization") == "Bearer tok"
    primary = google.build(Account("g", "calendar", "google", (("client_id", "cid"),)), secrets, context)
    assert primary.inner.calendar_id == "primary"
    with pytest.raises(AccountError):
        google.login(Account("g", "calendar", "google"), {}, context)
    assert google.BACKEND.key == "google" and google.BACKEND.login is not None and google.BACKEND.notes
    assert [s.key for s in google.BACKEND.settings] == ["client_id", "client_secret", "token", "calendar_id"]
    assert google.BACKEND.missing_settings(Account("g", "calendar", "google"), {}) == ("client_id", "token")
    assert google.BACKEND.missing_settings(account, secrets) == ()
