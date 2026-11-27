from dataclasses import replace
from datetime import date

import pytest

from jornada.cedb import PropVal
from jornada.pim import ids, store as pim_store
from jornada.pim.models import Task
from jornada.pim.store import DeviceStore
from jornada.pim.tasks import UNKNOWN_COMPLETION_DATE
from jornada.pim.timeconv import date_to_filetime
from jornada.rapi import RapiClient
from jornada.sync import registry
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import Item
from jornada.sync.engine import apply, plan, refresh_hashes
from jornada.sync.registry import BuildContext
from jornada.sync.state import SyncState
from jornada.sync.tasks import MODULE, device_store
from jornada.sync.tasks.filters import COMPLETED_SETTING, CompletedFilter, completed_mode, wrap_completed
from jornada.sync.tasks.todotxt import TodoFileStore
from tests.fake_device import FakeRapiServer
from tests.memory_store import MemoryStore

TODAY = date(2026, 9, 6)


@pytest.fixture
def device():
    server = FakeRapiServer()
    server.db.create(ids.DB_TASKS, records=[
        (PropVal.string(ids.SUBJECT, "From the device"), PropVal.filetime(ids.TASK_DUE, date_to_filetime(date(2026, 9, 10))),
         PropVal.i4(ids.IMPORTANCE, ids.IMPORTANCE_HIGH)),
    ])
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as connection:
        yield connection


def context(tmp_path, lines=None):
    log = lines.append if lines is not None else (lambda _line: None)
    return BuildContext(log=log, sync_dir=tmp_path / "sync", save_secrets=lambda _changes: None)


def test_module_spec_and_registry():
    assert MODULE.key == "tasks" and MODULE.title == "Tasks (Pocket Outlook Tasks)" and not MODULE.is_bridge
    assert [b.key for b in MODULE.backends] == ["apple", "mstodo", "gtasks", "caldav", "todotxt"]
    assert MODULE.settings == (COMPLETED_SETTING,) and COMPLETED_SETTING.default == "keep" and not COMPLETED_SETTING.required
    assert registry.load_modules({"tasks": "jornada.sync.tasks"})["tasks"] is MODULE
    assert MODULE.device_store is device_store
    for backend in MODULE.backends:
        assert "completed" not in {s.key for s in backend.settings} and backend.title and backend.notes
        assert all(spec.help for spec in backend.settings)


def test_completed_mode_validation_and_wrapping():
    assert completed_mode(Account("a", "tasks", "todotxt")) == "keep"
    assert completed_mode(Account("a", "tasks", "todotxt", (("completed", " Hide "),))) == "hide"
    with pytest.raises(AccountError):
        completed_mode(Account("a", "tasks", "todotxt", (("completed", "sometimes"),)))
    memory = MemoryStore()
    assert wrap_completed(memory, Account("a", "tasks", "todotxt")) is memory
    wrapped = wrap_completed(memory, Account("a", "tasks", "todotxt", (("completed", "hide"),)))
    assert isinstance(wrapped, CompletedFilter) and wrapped.inner is memory and wrapped.name == "memory"


def test_completed_filter_hides_completed_tasks_and_completes_instead_of_deleting():
    lines = []
    memory = MemoryStore({"open": Task("Open"), "done": Task("Done", completed=date(2026, 9, 1))})
    store = CompletedFilter(memory, log=lines.append, today=lambda: TODAY)
    assert [i.id for i in store.list()] == ["open"]
    assert store.create(Task("New")) == "m1" and memory.records["m1"] == Task("New")
    store.update("open", Task("Open edited"))
    assert memory.records["open"] == Task("Open edited")
    store.delete("open")
    assert memory.records["open"] == Task("Open edited", completed=TODAY) and "open" in memory.records
    assert lines[-1].endswith("completed instead of deleting it (completed=hide)")
    store.delete("done")                       # hidden: nothing to do
    store.delete("nope")
    assert memory.records["done"].completed == date(2026, 9, 1) and len(memory.records) == 3
    assert [i.id for i in store.list()] == ["m1"]
    fresh = CompletedFilter(MemoryStore({"x": Task("X")}), today=lambda: TODAY)
    fresh.delete("x")                          # never listed: the wrapper looks the task up first
    assert fresh.inner.records["x"].completed == TODAY


def test_device_store_is_the_tasks_database_with_the_filter(client, device, tmp_path, monkeypatch):
    monkeypatch.setattr(pim_store, "DEFAULT_SNAPSHOT_DIR", tmp_path / "snapshots")
    plain = device_store(client, Account("hand", "tasks", "todotxt", (("path", "x"),)), context(tmp_path))
    assert isinstance(plain, DeviceStore) and plain.database == ids.DB_TASKS
    (item,) = plain.list()
    assert item.record == Task("From the device", due=date(2026, 9, 10), priority="high")
    db = device.db.find(ids.DB_TASKS)
    device.db.add_record(db, (PropVal.string(ids.SUBJECT, "Finished"), PropVal.i2(ids.TASK_COMPLETED, 1)))
    hidden = device_store(client, Account("hand", "tasks", "todotxt", (("path", "x"), ("completed", "hide"))), context(tmp_path))
    assert isinstance(hidden, CompletedFilter) and [i.record.summary for i in hidden.list()] == ["From the device"]
    assert [i.record.summary for i in plain.list()] == ["From the device", "Finished"]
    assert plain.list()[1].record.completed == UNKNOWN_COMPLETION_DATE


def test_end_to_end_between_the_device_and_a_todo_txt_file(client, device, tmp_path, monkeypatch):
    monkeypatch.setattr(pim_store, "DEFAULT_SNAPSHOT_DIR", tmp_path / "snapshots")
    path = tmp_path / "todo.txt"
    path.write_text("Call mum due:2026-09-12\n", encoding="utf-8")
    account = Account("hand", "tasks", "todotxt", (("path", str(path)),))
    ctx = context(tmp_path)
    local = MODULE.device_store(client, account, ctx)
    remote = MODULE.backend(account.backend).build(account, {}, ctx)
    assert isinstance(remote, TodoFileStore)

    first = plan(local.list(), remote.list(), SyncState())
    assert sorted(a.kind for a in first.actions) == ["create_local", "create_remote"]
    state, result = apply(first, local, remote, SyncState())
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("Call mum due:2026-09-12 id:") and lines[1].startswith("(A) From the device due:2026-09-10 id:")
    db = device.db.find(ids.DB_TASKS)
    assert [p.value for props in db.records.values() for p in props if p.prop_id == ids.SUBJECT] == ["From the device", "Call mum"]
    assert list((tmp_path / "snapshots").glob("Tasks_Database.*.json"))
    assert plan(local.list(), remote.list(), state).is_empty

    device_oid = next(oid for oid, props in db.records.items() if any(p.value == "From the device" for p in props))
    db.records[device_oid] = db.records[device_oid] + (PropVal.filetime(ids.TASK_COMPLETED, date_to_filetime(date(2026, 9, 8))),)
    second = plan(local.list(), remote.list(), state)
    assert [a.kind for a in second.actions] == ["update_remote"]
    state, result = apply(second, local, remote, state)
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1].startswith("x 2026-09-08 (A) From the device due:2026-09-10 id:")
    assert plan(local.list(), remote.list(), state).is_empty

    path.write_text(path.read_text(encoding="utf-8").replace("Call mum due:2026-09-12", "(B) Call mum and dad due:2026-09-13"),
                    encoding="utf-8")
    third = plan(local.list(), remote.list(), state)
    assert [a.kind for a in third.actions] == ["update_local"]
    state, result = apply(third, local, remote, state)
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    mum = next(i for i in local.list() if i.record.summary == "Call mum and dad")
    assert mum.record.due == date(2026, 9, 13) and mum.record.priority == "high"
    assert plan(local.list(), remote.list(), state).is_empty
    assert len(state.links) == 2


def test_hide_mode_end_to_end_completes_instead_of_deleting(client, device, tmp_path, monkeypatch):
    monkeypatch.setattr(pim_store, "DEFAULT_SNAPSHOT_DIR", tmp_path / "snapshots")
    path = tmp_path / "todo.txt"
    path.write_text("x 2026-09-01 Done long ago\nCall mum due:2026-09-12\n", encoding="utf-8")
    account = Account("hand", "tasks", "todotxt", (("path", str(path)), ("completed", "hide")))
    ctx = context(tmp_path)
    local = MODULE.device_store(client, account, ctx)
    remote = MODULE.backend(account.backend).build(account, {}, ctx)
    first = plan(local.list(), remote.list(), SyncState())
    assert sorted(a.kind for a in first.actions) == ["create_local", "create_remote"]
    state, result = apply(first, local, remote, SyncState())
    assert not result.errors and "Done long ago" not in {i.record.summary for i in local.list()}
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)

    db = device.db.find(ids.DB_TASKS)
    mum_oid = next(oid for oid, props in db.records.items() if any(p.value == "Call mum" for p in props))
    db.records[mum_oid] = db.records[mum_oid] + (PropVal.filetime(ids.TASK_COMPLETED, date_to_filetime(date(2026, 9, 8))),)
    second = plan(local.list(), remote.list(), state)
    assert [a.kind for a in second.actions] == ["delete_remote"]
    state, result = apply(second, local, remote, state)
    assert not result.errors
    lines = path.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("x ") and "Call mum" in line for line in lines)      # completed, not deleted
    assert plan(local.list(), remote.list(), state).is_empty and len(state.links) == 1


def test_completed_filter_forgets_a_task_it_completed_itself():
    lines = []
    memory = MemoryStore({"a": Task("A")})
    store = CompletedFilter(memory, log=lines.append, today=lambda: TODAY)
    store.list()
    store.update("a", Task("A", completed=date(2026, 9, 2)))     # completed through the wrapper: no longer open
    store.delete("a")                                            # nothing left to complete
    assert memory.records["a"].completed == date(2026, 9, 2) and memory.calls.count(("update", "a")) == 1
    assert lines[-1] == "memory: a is already gone or completed; nothing to delete"
