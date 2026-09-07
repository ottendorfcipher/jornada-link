from datetime import date, datetime

import pytest

from jornada.pim.models import Appointment, Task
from jornada.sync.base import Item
from jornada.sync.engine import Direction, Options, Prefer, apply, plan, refresh_hashes
from jornada.sync.state import Link, SyncState
from tests.memory_store import MemoryStore


def appt(summary, hour=9, notes=""):
    return Appointment(summary, datetime(2026, 9, 7, hour), datetime(2026, 9, 7, hour + 1), notes=notes)


def items(store):
    return store.list()


def linked(local_id, remote_id, record):
    return Link(local_id, remote_id, record.fingerprint(), record.fingerprint())


def test_first_sync_creates_both_ways_and_pairs_matches():
    local = MemoryStore({"1": appt("Dentist"), "2": appt("Lunch", 12)})
    remote = MemoryStore({"r1": appt("Lunch", 12), "r2": appt("Gym", 18)})
    p = plan(items(local), items(remote), SyncState())
    kinds = sorted((a.kind, a.local_id, a.remote_id) for a in p.actions)
    assert kinds == [("create_local", None, "r2"), ("create_remote", "1", None), ("link", "2", "r1")]
    state, result = apply(p, local, remote, SyncState())
    assert result.counts == {"create_remote": 1, "link": 1, "create_local": 1} and not result.errors
    assert {l.local_id: l.remote_id for l in state.links} == {"1": "m1", "2": "r1", "m1": "r2"}
    assert remote.records["m1"] == appt("Dentist") and local.records["m1"] == appt("Gym", 18)
    assert state.last_sync


def test_paired_records_with_different_content_follow_preference():
    local = MemoryStore({"1": appt("Lunch", 12, notes="device")})
    remote = MemoryStore({"r1": appt("Lunch", 12, notes="cloud")})
    p = plan(items(local), items(remote), SyncState())
    assert [a.kind for a in p.actions] == ["link", "update_local"]
    p2 = plan(items(local), items(remote), SyncState(), Options(prefer=Prefer.LOCAL))
    assert [a.kind for a in p2.actions] == ["link", "update_remote"]


def test_steady_state_is_empty_then_changes_flow_each_way():
    local = MemoryStore({"1": appt("A")})
    remote = MemoryStore({"r1": appt("A")})
    state = SyncState((linked("1", "r1", appt("A")),))
    assert plan(items(local), items(remote), state).is_empty
    local.records["1"] = appt("A", notes="edited on device")
    p = plan(items(local), items(remote), state)
    assert [(a.kind, a.reason) for a in p.actions] == [("update_remote", "changed on the device")]
    state, _ = apply(p, local, remote, state)
    assert remote.records["r1"].notes == "edited on device"
    remote.records["r1"] = appt("A", notes="edited remotely")
    p = plan(items(local), items(remote), state)
    assert [a.kind for a in p.actions] == ["update_local"]
    state, _ = apply(p, local, remote, state)
    assert local.records["1"].notes == "edited remotely"
    assert plan(items(local), items(remote), state).is_empty


def test_conflict_resolution_and_identical_double_edit():
    record = appt("A")
    state = SyncState((linked("1", "r1", record),))
    local = MemoryStore({"1": appt("A", notes="x")})
    remote = MemoryStore({"r1": appt("A", notes="y")})
    assert plan(items(local), items(remote), state).actions[0].kind == "update_local"
    assert plan(items(local), items(remote), state, Options(prefer=Prefer.LOCAL)).actions[0].kind == "update_remote"
    from_device = plan(items(local), items(remote), state, Options(direction=Direction.FROM_DEVICE))
    assert from_device.actions[0].kind == "update_remote"   # one-way: the source side wins
    to_device = plan(items(local), items(remote), state,
                     Options(direction=Direction.TO_DEVICE, prefer=Prefer.LOCAL))
    assert to_device.actions[0].kind == "update_local"
    same = MemoryStore({"r1": appt("A", notes="x")})
    assert plan(items(local), items(same), state).actions[0].kind == "link"


def test_deletions_propagate_or_unlink():
    record = appt("A")
    state = SyncState((linked("1", "r1", record), linked("2", "r2", appt("B"))))
    local = MemoryStore({"1": record})            # "2" deleted on device
    remote = MemoryStore({"r2": appt("B")})       # "r1" deleted remotely
    p = plan(items(local), items(remote), state)
    assert sorted(a.kind for a in p.actions) == ["delete_local", "delete_remote"]
    new_state, result = apply(p, local, remote, state)
    assert local.records == {} and remote.records == {} and new_state.links == ()
    kept = plan(items(MemoryStore({"1": record})), items(MemoryStore({"r2": appt("B")})), state,
                Options(propagate_deletes=False))
    assert sorted(a.kind for a in kept.actions) == ["unlink", "unlink"]
    gone = plan((), (), state)
    assert [a.kind for a in gone.actions] == ["unlink", "unlink"]


def test_one_way_directions_never_write_the_other_side():
    local = MemoryStore({"1": appt("Device only")})
    remote = MemoryStore({"r1": appt("Remote only")})
    to_device = plan(items(local), items(remote), SyncState(), Options(direction=Direction.TO_DEVICE))
    assert sorted(a.kind for a in to_device.actions) == ["create_local", "skip"]
    from_device = plan(items(local), items(remote), SyncState(), Options(direction=Direction.FROM_DEVICE))
    assert sorted(a.kind for a in from_device.actions) == ["create_remote", "skip"]
    assert not to_device.writes_device or from_device.writes_device is False


def test_apply_continues_after_errors_and_reports_them():
    local = MemoryStore({"1": appt("A")}, fail_on=("create",))
    remote = MemoryStore({"r1": appt("B", 10), "r2": appt("C", 11)})
    p = plan(items(local), items(remote), SyncState())
    state, result = apply(p, local, remote, SyncState())
    assert result.counts.get("create_remote") == 1 and len(result.errors) == 2
    assert all("refused" in e for e in result.errors)
    assert len(state.links) == 1


def test_refresh_hashes_records_what_each_side_stored():
    stored = appt("A", notes="normalized")
    state = SyncState((Link("1", "r1", "old", "old"),))
    refreshed = refresh_hashes(state, (Item("1", stored),), (Item("r1", stored),), {"1"}, set())
    assert refreshed.links[0].local_hash == stored.fingerprint() and refreshed.links[0].remote_hash == "old"


def test_plan_summary_and_describe():
    p = plan((Item("1", Task("Buy milk", due=date(2026, 1, 1))),), (), SyncState())
    assert p.summary() == "1 create remote" and "Buy milk" in p.actions[0].describe()
    assert plan((), (), SyncState()).summary() == "nothing to do"
