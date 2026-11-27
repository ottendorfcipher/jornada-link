"""The Apple Calendar backend against a fake osascript runner: the argv / JSON contract of the four
scripts, alarms, calendar creation and the rule that user text never enters script source."""
import time
from dataclasses import replace
from datetime import datetime

import pytest

from jornada.pim.models import Appointment
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.calendar import apple
from jornada.sync.calendar.apple import (CREATE_SCRIPT, DELETE_SCRIPT, LIST_SCRIPT, UPDATE_SCRIPT, AppleCalendarStore,
                                         event_arguments, event_record, event_span, reminder_from_trigger)
from jornada.sync.calendar.window import WindowedStore
from jornada.sync.registry import BuildContext
from jornada.webapi.applescript import fake_runner

OSASCRIPT = ("/usr/bin/osascript", "-l", "JavaScript", "-")
START, END = datetime(2026, 8, 8), datetime(2027, 9, 8)
UID = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
DENTIST = Appointment("Dentist", datetime(2026, 9, 7, 9, 30), datetime(2026, 9, 7, 10, 15), location="Clinic",
                      notes="bring card", reminder_minutes=15)
DENTIST_ARGS = ("Dentist", "2026-09-07T09:30:00+02:00", "2026-09-07T10:15:00+02:00", "0", "Clinic", "bring card", "15")


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    """Calendar.app answers in UTC; the expected wall-clock values below assume one fixed zone."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def entry(**overrides):
    base = {"uid": UID, "summary": "Dentist", "startDate": "2026-09-07T07:30:00.000Z",
            "endDate": "2026-09-07T08:15:00.000Z", "alldayEvent": False, "location": "Clinic",
            "description": "bring card", "recurrence": "", "stamp": "2026-09-01T10:00:00.000Z", "alarmMinutes": -15}
    return {**base, **overrides}


def store_with(answers, calls=None, lines=None, calendar="Jornada"):
    log = lines.append if lines is not None else (lambda _line: None)
    return AppleCalendarStore(START, END, calendar=calendar, runner=fake_runner(answers, calls), log=log)


# -- pure mapping --------------------------------------------------------------------
def test_pure_mapping_helpers():
    assert [reminder_from_trigger(v) for v in (None, True, "x", -30, 0, 5, "-45")] == [None, None, None, 30, 0, 0, 45]
    assert event_span(datetime(2026, 9, 7, 9), datetime(2026, 9, 7, 8), False) == (datetime(2026, 9, 7, 9),) * 2
    assert event_span(datetime(2026, 9, 10, 0, 0, 1), datetime(2026, 9, 11, 23, 59, 59), True) == (datetime(2026, 9, 10),
                                                                                                    datetime(2026, 9, 12))
    assert event_span(datetime(2026, 9, 10), datetime(2026, 9, 12), True) == (datetime(2026, 9, 10), datetime(2026, 9, 12))
    assert event_record(entry()) == replace(DENTIST, uid=UID)
    sparse = event_record(entry(alarmMinutes=None, recurrence="FREQ=DAILY", summary=None, location=None, description=None))
    assert sparse.reminder_minutes is None and sparse.recurring and (sparse.summary, sparse.location, sparse.notes) == ("",) * 3
    with pytest.raises(ValueError):
        event_record(entry(endDate=None))
    with pytest.raises(ValueError):
        event_record(entry(startDate=" "))
    assert event_arguments(DENTIST) == DENTIST_ARGS
    assert event_arguments(replace(DENTIST, reminder_minutes=None, end=datetime(2026, 9, 7, 8)))[1:4] == (
        "2026-09-07T09:30:00+02:00", "2026-09-07T09:30:00+02:00", "0")            # an end before the start is clamped
    assert event_arguments(replace(DENTIST, reminder_minutes=None))[6] == ""
    assert event_arguments(replace(DENTIST, reminder_minutes=-5))[6] == "0"
    board = Appointment(" Board ", datetime(2026, 9, 10, 8), datetime(2026, 9, 11, 17), all_day=True, notes="a\r\nb")
    assert event_arguments(board) == ("Board", "2026-09-10T00:00:00+02:00", "2026-09-11T23:59:59+02:00", "1", "", "a\nb", "")


# -- listing ------------------------------------------------------------------------------
def test_list_passes_the_calendar_and_window_as_argv_and_maps_the_entries():
    calls, lines = [], []
    answers = [[entry(),
                entry(uid="all-day", summary="Board", startDate="2026-09-09T22:00:00.000Z", location="",
                      endDate="2026-09-11T21:59:59.000Z", alldayEvent=True, description="", alarmMinutes=None, stamp=""),
                entry(uid="weekly", summary="Standup", recurrence="FREQ=WEEKLY;INTERVAL=1", alarmMinutes=0),
                entry(uid="broken", startDate="")]]
    store = store_with(answers, calls, lines, calendar="Handheld")
    dentist, board, standup, broken = store.list()
    argv, stdin, _timeout = calls[0]
    assert argv == OSASCRIPT + ("Handheld", "2026-08-08T00:00:00+02:00", "2027-09-08T00:00:00+02:00")
    assert stdin == LIST_SCRIPT
    assert dentist.id == UID and dentist.version == "2026-09-01T10:00:00.000Z"
    assert dentist.record == replace(DENTIST, uid=UID)
    assert board.record == Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 12), all_day=True, uid="all-day")
    assert board.version is None
    assert standup.record.recurring and standup.read_only and standup.record.reminder_minutes == 0
    assert not dentist.read_only and not board.read_only
    assert (broken.id, broken.unreadable, broken.problem, broken.version) == ("broken", True, "missing date", "2026-09-01T10:00:00.000Z")
    assert lines == ["Calendar event broken cannot be read (missing date); it is left alone"]
    assert store.name == "apple-calendar" and store.calendar == "Handheld"
    doomsday = entry(uid="end", alldayEvent=True, startDate="9999-12-31T00:00:00.000Z", endDate="9999-12-31T23:59:59.000Z")
    listed = store_with([[doomsday, entry()]], lines=lines).list()
    assert [(item.id, item.unreadable) for item in listed] == [("end", True), (UID, False)]
    assert lines[-1] == "Calendar event end cannot be read (date value out of range); it is left alone"
    assert store_with([]).calendar == "Jornada" and store_with([], calendar="  ").calendar == "Jornada"


# -- writing ------------------------------------------------------------------------------
def test_create_sends_the_fields_as_argv_and_returns_the_new_uid():
    calls = []
    store = store_with([entry(), entry(uid="day-uid")], calls)
    assert store.create(replace(DENTIST, summary=" Dentist ", categories=("lost",), private=True)) == UID
    argv, stdin, _ = calls[0]
    assert stdin == CREATE_SCRIPT and argv[:4] == OSASCRIPT
    assert argv[4:] == ("Jornada",) + DENTIST_ARGS
    board = Appointment("Board", datetime(2026, 9, 10, 8), datetime(2026, 9, 11, 17), all_day=True)
    assert store.create(board) == "day-uid"
    assert calls[1][0][5:] == ("Board", "2026-09-10T00:00:00+02:00", "2026-09-11T23:59:59+02:00", "1", "", "", "")


def test_update_and_delete_address_the_event_by_uid():
    calls, lines = [], []
    answers = [[entry(uid="weekly", recurrence="FREQ=DAILY")], entry(stamp="stamp-2"), {"uid": UID},
               {"uid": UID, "deleted": True}, {"uid": "gone", "deleted": False}]
    store = store_with(answers, calls, lines, calendar="Handheld")
    store.list()
    with pytest.raises(StoreError) as info:
        store.update("weekly", DENTIST)
    assert "recurring series in Calendar" in str(info.value) and len(calls) == 1
    assert store.update(UID, DENTIST) == "stamp-2"
    argv, stdin, _ = calls[1]
    assert stdin == UPDATE_SCRIPT and argv[4:6] == ("Handheld", UID) and argv[6:] == DENTIST_ARGS
    assert store.update(UID, DENTIST) is None                       # no stamp reported: no version either
    store.delete(UID)
    assert calls[3][1] == DELETE_SCRIPT and calls[3][0][4:] == ("Handheld", UID)
    store.delete("gone")
    assert lines == ["event gone was already gone from Calendar"]


def test_the_recurring_flag_follows_the_latest_listing():
    calls = []
    store = store_with([[entry(recurrence="FREQ=DAILY")], [entry()], entry()], calls)
    store.list()
    with pytest.raises(StoreError):
        store.update(UID, DENTIST)
    store.list()                                                    # the series became a one-off
    assert store.update(UID, DENTIST) == "2026-09-01T10:00:00.000Z" and len(calls) == 3


# -- the scripts themselves ------------------------------------------------------------------
def test_scripts_are_constants_that_never_carry_user_text():
    for script in (LIST_SCRIPT, CREATE_SCRIPT, UPDATE_SCRIPT, DELETE_SCRIPT):
        assert "function run(argv)" in script and "JSON.stringify" in script
        assert "Jornada" not in script and "Dentist" not in script and "Handheld" not in script
        assert "calendars.push" in script                           # the calendar is created when missing
    assert "whose" in LIST_SCRIPT and "startDate" in LIST_SCRIPT and "endDate" in LIST_SCRIPT
    assert "DisplayAlarm" in CREATE_SCRIPT and "DisplayAlarm" in UPDATE_SCRIPT and "triggerInterval" in _PRELUDE_OF(LIST_SCRIPT)
    assert "eventWithUid" in UPDATE_SCRIPT and "eventWithUid" in DELETE_SCRIPT and "delete()" in DELETE_SCRIPT


def _PRELUDE_OF(script):
    return script.split("function run(argv)")[0]


# -- failures ---------------------------------------------------------------------------------
def test_failures_become_store_errors():
    answers = [RuntimeError("Calendar got an error: Not authorized to send Apple events"), {"nope": 1},
               [{"summary": "no uid"}], {"uid": ""}, "not json"]
    store = store_with(answers)
    with pytest.raises(StoreError) as info:
        store.list()
    assert str(info.value).startswith("Calendar could not list the calendar 'Jornada': ")
    assert "Not authorized" in str(info.value)
    with pytest.raises(StoreError, match="unexpected listing"):
        store.list()                                                # an object instead of a list
    with pytest.raises(StoreError, match="uid"):
        store.list()                                                # an entry without a uid
    with pytest.raises(StoreError, match="uid of the event it should create"):
        store.create(DENTIST)
    with pytest.raises(StoreError, match="could not create the event 'Dentist'"):
        store.create(DENTIST)                                       # not JSON
    with pytest.raises(StoreError, match="needs an event uid"):
        store.update(" ", DENTIST)
    with pytest.raises(StoreError, match="needs an event uid"):
        store.delete("")
    with pytest.raises(StoreError):
        store.delete(UID)                                           # no canned answer left


# -- backend spec ------------------------------------------------------------------------------
def test_build_reads_the_calendar_setting_and_applies_the_window(tmp_path):
    calls = []
    listing = [entry(), entry(uid="old", startDate="2020-01-01T09:00:00.000Z", endDate="2020-01-01T10:00:00.000Z"),
               entry(uid="weekly", recurrence="FREQ=WEEKLY")]
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           runner=fake_runner([listing, listing], calls), extra={"now": datetime(2026, 9, 1, 12)})
    store = apple.build(Account("a", "calendar", "apple", (("calendar", "Handheld"),)), {}, context)
    assert isinstance(store, WindowedStore) and isinstance(store.inner, AppleCalendarStore)
    assert store.window == (datetime(2026, 8, 2), datetime(2027, 9, 2)) and store.skip_recurring
    assert [item.id for item in store.list()] == [UID]
    assert calls[0][0][4:] == ("Handheld", "2026-08-02T00:00:00+02:00", "2027-09-02T00:00:00+02:00")
    shown = apple.build(Account("a", "calendar", "apple", (("recurring", "first"),)), {}, context)
    assert shown.inner.calendar == "Jornada" and [item.id for item in shown.list()] == [UID, "weekly"]
    assert apple.BACKEND.key == "apple" and apple.BACKEND.login is None and apple.BACKEND.notes
    assert [(s.key, s.required, s.default) for s in apple.BACKEND.settings] == [("calendar", False, "Jornada")]
    assert apple.BACKEND.missing_settings(Account("a", "calendar", "apple"), {}) == ()
