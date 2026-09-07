"""The calendar module: its spec, the windowed device store, and a full plan / apply / refresh cycle
between the Appointments Database on the fake device and a CalDAV calendar on the fake server."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from jornada.cedb import PropVal
from jornada.pim import ids
from jornada.pim.appointments import duration_for_days
from jornada.pim.models import Appointment
from jornada.pim.notes_blob import encode_notes
from jornada.pim.store import DeviceStore
from jornada.pim.timeconv import filetime_to_naive, naive_to_filetime
from jornada.rapi import RapiClient
from jornada.sync import registry
from jornada.sync.accounts import Account, AccountError
from jornada.sync.calendar import MODULE, SNAPSHOT_SUBDIR, device_store
from jornada.sync.calendar.caldav import CalDavCalendarStore
from jornada.sync.calendar.common import local_wall_clock
from jornada.sync.calendar.icalmap import from_vevent
from jornada.sync.calendar.window import WindowedStore
from jornada.sync.engine import apply, plan, refresh_hashes
from jornada.sync.registry import BuildContext
from jornada.sync.state import SyncState
from jornada.webapi import ical
from tests.fake_dav import EVENT_A, PASSWORD, USERNAME, Item
from tests.fake_device import FakeRapiServer
from tests.test_calendar_caldav import STANDUP_ICS, WORK, calendar_server, vcalendar

NOW = datetime(2026, 3, 1, 12)
WINDOW = (datetime(2026, 1, 30), datetime(2027, 3, 2))
UTC = timezone.utc
DENTIST = Appointment("Dentist", datetime(2026, 9, 7, 9, 30), datetime(2026, 9, 7, 10, 15), location="Clinic",
                      notes="bring card", categories=("Health",), busy_status="tentative", private=True,
                      reminder_minutes=15)
RETREAT_ICS = vcalendar("UID:retreat", "DTSTAMP:20260301T000000Z", "DTSTART;VALUE=DATE:20260601",
                        "DTEND;VALUE=DATE:20260604", "SUMMARY:Retreat", "TRANSP:TRANSPARENT")


def timed(subject, start, minutes, *extra):
    return (PropVal.string(ids.SUBJECT, subject), PropVal.filetime(ids.APPT_START, naive_to_filetime(start)),
            PropVal.i4(ids.APPT_DURATION, minutes), PropVal.i4(ids.APPT_TYPE, ids.APPT_TYPE_NORMAL), *extra)


DENTIST_PROPS = timed("Dentist", datetime(2026, 9, 7, 9, 30), 45, PropVal.string(ids.APPT_LOCATION, "Clinic"),
                      PropVal.blob(ids.NOTES, encode_notes("bring card")), PropVal.i2(ids.REMINDER_ENABLED, 1),
                      PropVal.i4(ids.REMINDER_MINUTES, 15), PropVal.i2(ids.APPT_BUSY_STATUS, ids.BUSY_TENTATIVE),
                      PropVal.i2(ids.SENSITIVITY, ids.SENSITIVITY_PRIVATE), PropVal.string(ids.CATEGORIES, "Health"))
BOARD_PROPS = (PropVal.string(ids.SUBJECT, "Board"), PropVal.filetime(ids.APPT_START, naive_to_filetime(datetime(2026, 9, 10))),
               PropVal.i4(ids.APPT_DURATION, duration_for_days(2)), PropVal.i4(ids.APPT_TYPE, ids.APPT_TYPE_ALL_DAY))
WEEKLY_PROPS = timed("Weekly", datetime(2026, 9, 8, 9), 30, PropVal.i2(ids.APPT_OCCURRENCE, ids.OCCURRENCE_REPEATED))
ANCIENT_PROPS = timed("Ancient", datetime(2020, 1, 1, 9), 60)


@pytest.fixture
def device():
    server = FakeRapiServer()
    server.db.create(ids.DB_APPOINTMENTS, records=[DENTIST_PROPS, BOARD_PROPS, WEEKLY_PROPS, ANCIENT_PROPS])
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as connection:
        yield connection


@pytest.fixture
def dav_server():
    server = calendar_server((("standup", STANDUP_ICS), ("retreat", RETREAT_ICS)))
    try:
        yield server
    finally:
        server.stop()


def context(tmp_path, lines=None):
    log = lines.append if lines is not None else (lambda _line: None)
    return BuildContext(log=log, sync_dir=tmp_path / "sync", save_secrets=lambda _changes: None, extra={"now": NOW})


def build_stores(client, dav_server, tmp_path, lines=None, **settings):
    base = (("url", dav_server.base_url), ("username", USERNAME), ("calendar", "Work"))
    account = Account("hand", "calendar", "caldav", base + tuple(settings.items()))
    ctx = context(tmp_path, lines)
    local = MODULE.device_store(client, account, ctx)
    remote = MODULE.backend(account.backend).build(account, {"password": PASSWORD}, ctx)
    assert isinstance(local, WindowedStore) and isinstance(remote, WindowedStore)
    assert isinstance(remote.inner, CalDavCalendarStore) and local.window == remote.window == WINDOW
    return local, remote


def sync(local, remote, state, log=lambda _line: None):
    """One run of the engine: plan, apply, then refresh the hashes from what both sides stored."""
    plan_ = plan(local.list(), remote.list(), state)
    state, result = apply(plan_, local, remote, state, log=log)
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    return plan_, state, result


@pytest.fixture
def synced(client, device, dav_server, tmp_path):
    local, remote = build_stores(client, dav_server, tmp_path)
    _first, state, result = sync(local, remote, SyncState())
    assert not result.errors
    return local, remote, state


# -- looking at both sides ------------------------------------------------------------------
def db_of(device):
    return device.db.find(ids.DB_APPOINTMENTS)


def oid_of(device, subject):
    records = db_of(device).records.items()
    return next(str(oid) for oid, props in records if any(p.prop_id == ids.SUBJECT and p.value == subject for p in props))


def prop_of(device, subject, prop_id):
    props = db_of(device).records[int(oid_of(device, subject))]
    return next((p.value for p in props if p.prop_id == prop_id), None)


def subjects(device):
    return [next(p.value for p in props if p.prop_id == ids.SUBJECT) for props in db_of(device).records.values()]


def set_props(device, subject, *changed):
    db, oid = db_of(device), int(oid_of(device, subject))
    replaced = {p.prop_id for p in changed}
    db.records[oid] = tuple(p for p in db.records[oid] if p.prop_id not in replaced) + changed


def remote_id(state, device, subject):
    return next(link.remote_id for link in state.links if link.local_id == oid_of(device, subject))


def server_event(dav_server, path):
    return ical.parse(dav_server.items[path].data).find("VEVENT")


def server_summaries(dav_server):
    return sorted(server_event(dav_server, path).value("SUMMARY") for path in dav_server.items if path.startswith(WORK))


def kinds(plan_):
    return sorted((action.kind, action.record.summary) for action in plan_.actions)


# -- the spec ---------------------------------------------------------------------------------
def test_module_spec_and_registry():
    assert MODULE.key == "calendar" and MODULE.title == "Calendar (Pocket Outlook Appointments)" and not MODULE.is_bridge
    assert [backend.key for backend in MODULE.backends] == ["apple", "google", "m365", "caldav"]
    assert [(s.key, s.required, s.default) for s in MODULE.settings] == [
        ("window_past", False, "30"), ("window_future", False, "365"), ("recurring", False, "skip")]
    assert registry.load_modules({"calendar": "jornada.sync.calendar"})["calendar"] is MODULE
    assert MODULE.device_store is device_store and MODULE.notes
    module_keys = {s.key for s in MODULE.settings}
    for backend in MODULE.backends:
        assert backend.title and backend.notes and all(s.help for s in backend.settings)
        assert not module_keys & {s.key for s in backend.settings}
    assert MODULE.backend("caldav").missing_settings(Account("a", "calendar", "caldav"), {}) == ("url", "username", "password")
    with pytest.raises(AccountError):
        MODULE.backend("outlook-express")


def test_device_store_is_the_windowed_appointments_database(client, device, tmp_path):
    lines = []
    store = device_store(client, Account("hand", "calendar", "caldav"), context(tmp_path, lines))
    assert isinstance(store, WindowedStore) and isinstance(store.inner, DeviceStore)
    assert store.inner.database == ids.DB_APPOINTMENTS and store.window == WINDOW and store.skip_recurring
    assert [item.record.summary for item in store.list()] == ["Dentist", "Board"]
    assert [item.record.summary for item in store.inner.list()] == ["Dentist", "Board", "Weekly", "Ancient"]
    assert [item.read_only for item in store.inner.list()] == [False, False, True, False]
    assert store.list()[0].record == DENTIST
    assert store.list()[1].record == Appointment("Board", datetime(2026, 9, 10), datetime(2026, 9, 12), all_day=True)
    wide = device_store(client, Account("hand", "calendar", "caldav", (("recurring", "first"), ("window_past", "3000"))), context(tmp_path))
    assert [item.record.summary for item in wide.list()] == ["Dentist", "Board", "Weekly", "Ancient"]
    with pytest.raises(AccountError, match="window_future"):
        device_store(client, Account("hand", "calendar", "caldav", (("window_future", "soon"),)), context(tmp_path))
    store.create(Appointment("New", datetime(2026, 9, 20, 9), datetime(2026, 9, 20, 10)))
    assert list((tmp_path / "sync" / SNAPSHOT_SUBDIR).glob("Appointments_Database.*.json"))
    assert any("snapshot" in line for line in lines) and "New" in subjects(device)


# -- end to end ---------------------------------------------------------------------------------
def test_first_sync_creates_both_ways_and_maps_every_field(client, device, dav_server, tmp_path):
    lines = []
    local, remote = build_stores(client, dav_server, tmp_path, lines)
    first, state, result = sync(local, remote, SyncState(), lines.append)
    assert kinds(first) == [("create_local", "April meeting"), ("create_local", "March meeting"), ("create_local", "Retreat"),
                            ("create_remote", "Board"), ("create_remote", "Dentist")]
    assert not result.errors and result.counts == {"create_local": 3, "create_remote": 2}
    assert plan(local.list(), remote.list(), state).is_empty and len(state.links) == 5
    dentist = from_vevent(server_event(dav_server, remote_id(state, device, "Dentist")))
    assert dentist.uid and dentist == replace(DENTIST, uid=dentist.uid)
    board = server_event(dav_server, remote_id(state, device, "Board"))
    assert (board.value("DTSTART"), board.value("DTEND")) == ("20260910", "20260912")
    assert subjects(device) == ["Dentist", "Board", "Weekly", "Ancient", "March meeting", "April meeting", "Retreat"]
    march_start = filetime_to_naive(prop_of(device, "March meeting", ids.APPT_START))
    assert march_start == local_wall_clock(datetime(2026, 3, 10, 9, tzinfo=UTC))
    assert prop_of(device, "March meeting", ids.APPT_DURATION) == 60 and prop_of(device, "March meeting", ids.APPT_OCCURRENCE) == 0
    assert prop_of(device, "Retreat", ids.APPT_TYPE) == ids.APPT_TYPE_ALL_DAY
    assert prop_of(device, "Retreat", ids.APPT_DURATION) == duration_for_days(3)
    assert prop_of(device, "Retreat", ids.APPT_BUSY_STATUS) == ids.BUSY_FREE
    assert list((tmp_path / "sync" / SNAPSHOT_SUBDIR).glob("Appointments_Database.*.json"))
    assert any(line.startswith("→ remote") for line in lines) and any(line.startswith("snapshot") for line in lines)


def test_a_device_edit_flows_to_the_server(synced, device, dav_server):
    local, remote, state = synced
    set_props(device, "Dentist", PropVal.string(ids.APPT_LOCATION, "Room 2"), PropVal.i4(ids.APPT_DURATION, 60))
    second, state, result = sync(local, remote, state)
    assert kinds(second) == [("update_remote", "Dentist")] and not result.errors
    path = remote_id(state, device, "Dentist")
    event = server_event(dav_server, path)
    assert event.value("LOCATION") == "Room 2" and event.value("DTEND") == ical.format_datetime(
        datetime(2026, 9, 7, 10, 30).astimezone())
    assert event.value("UID") == from_vevent(event).uid and event.value("DESCRIPTION") == "bring card"
    put_path, headers = [(p, h) for verb, p, h in dav_server.requests if verb == "PUT"][-1]
    assert put_path == path and headers.get("If-Match") and headers.get("If-Match") != dav_server.items[path].etag
    assert plan(local.list(), remote.list(), state).is_empty


def test_a_server_edit_flows_to_the_device(synced, device, dav_server):
    local, remote, state = synced
    moved = (EVENT_A.replace("SUMMARY:March meeting", "SUMMARY:March meeting (moved)")
             .replace("DTSTART:20260310T090000Z", "DTSTART:20260311T090000Z")
             .replace("DTEND:20260310T100000Z", "DTEND:20260311T103000Z"))
    dav_server.items[f"{WORK}a.ics"] = Item('"etag-a-2"', moved, "text/calendar; charset=utf-8")
    second, state, result = sync(local, remote, state)
    assert kinds(second) == [("update_local", "March meeting (moved)")] and not result.errors
    assert "March meeting" not in subjects(device)
    start = filetime_to_naive(prop_of(device, "March meeting (moved)", ids.APPT_START))
    assert start == local_wall_clock(datetime(2026, 3, 11, 9, tzinfo=UTC))
    assert prop_of(device, "March meeting (moved)", ids.APPT_DURATION) == 90
    assert plan(local.list(), remote.list(), state).is_empty and len(state.links) == 5


def test_deletions_propagate_both_ways(synced, device, dav_server):
    local, remote, state = synced
    del dav_server.items[f"{WORK}b.ics"]
    del db_of(device).records[int(oid_of(device, "Board"))]
    second, state, result = sync(local, remote, state)
    assert kinds(second) == [("delete_local", "April meeting"), ("delete_remote", "Board")] and not result.errors
    assert "April meeting" not in subjects(device) and "Board" not in server_summaries(dav_server)
    assert len(state.links) == 3 and plan(local.list(), remote.list(), state).is_empty


def test_recurring_and_out_of_window_records_are_left_alone(synced, device, dav_server):
    local, remote, state = synced
    linked_local, linked_remote = {l.local_id for l in state.links}, {l.remote_id for l in state.links}
    assert oid_of(device, "Weekly") not in linked_local and oid_of(device, "Ancient") not in linked_local
    assert f"{WORK}standup.ics" not in linked_remote
    assert "Weekly" not in server_summaries(dav_server) and "Ancient" not in server_summaries(dav_server)
    assert "Standup" not in subjects(device)
    assert db_of(device).records[int(oid_of(device, "Weekly"))] == WEEKLY_PROPS
    moved = STANDUP_ICS.replace("SUMMARY:Standup", "SUMMARY:Standup (moved)")
    dav_server.items[f"{WORK}standup.ics"] = Item('"etag-standup-2"', moved, "text/calendar; charset=utf-8")
    set_props(device, "Weekly", PropVal.string(ids.APPT_LOCATION, "Elsewhere"))
    second, state, result = sync(local, remote, state)
    assert second.actions == () and not result.errors and len(state.links) == 5


def test_recurring_first_syncs_masters_as_one_offs_but_never_rewrites_the_device_series(client, device, dav_server, tmp_path):
    local, remote = build_stores(client, dav_server, tmp_path, recurring="first")
    first, state, result = sync(local, remote, SyncState())
    assert {("create_remote", "Weekly"), ("create_local", "Standup")} <= set(kinds(first)) and not result.errors
    weekly_path = remote_id(state, device, "Weekly")
    assert server_event(dav_server, weekly_path).get("RRULE") is None                   # a one-off copy
    assert prop_of(device, "Standup", ids.APPT_OCCURRENCE) == ids.OCCURRENCE_ONCE
    assert plan(local.list(), remote.list(), state).is_empty
    moved = dav_server.items[weekly_path].data.replace("SUMMARY:Weekly", "SUMMARY:Weekly (moved)")
    dav_server.items[weekly_path] = Item('"etag-weekly-2"', moved, "text/calendar; charset=utf-8")
    second, state, result = sync(local, remote, state)
    assert [(a.kind, a.reason) for a in second.actions] == [("skip", "changed remotely; device record is read-only")]
    assert second.is_empty and not result.errors
    assert db_of(device).records[int(oid_of(device, "Weekly"))] == WEEKLY_PROPS
