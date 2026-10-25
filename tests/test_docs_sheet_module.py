import sqlite3

import pytest

from jornada.cedb import CEVT_I4, CEVT_LPWSTR, PropVal
from jornada.pim.filestore import DeviceFolderStore
from jornada.pim.models import Document
from jornada.rapi import RapiClient
from jornada.sendmirror import ENV_MIRROR_DIR
from jornada.sync import documents
from jornada.sync.accounts import Account
from jornada.sync.documents import sheet_backends
from jornada.sync.documents.sqlite_tables import SqliteTableStore
from jornada.sync.documents.tablestore import SCHEMA_PROP, DeviceTableStore
from jornada.sync.engine import Options, Prefer, apply, plan, refresh_hashes
from jornada.sync.registry import BuildContext
from jornada.sync.state import SyncState
from tests.fake_device import FakeRapiServer


@pytest.fixture
def device():
    server = FakeRapiServer()
    server.db.create("Inventory", records=[(PropVal.string(SCHEMA_PROP, "Part,Count"),),
                                           (PropVal.string(1, "Bolt"), PropVal.i4(2, 40))])
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as c:
        yield c


def context(tmp_path):
    return BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _changes: None)


def test_family_registration_and_device_store_dispatch(client, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    assert [b.key for b in sheet_backends.BACKENDS] == ["excel", "sheets", "numbers", "sqlite"]
    assert sheet_backends.DEVICE_STORE is sheet_backends.device_store
    module = documents.MODULE
    assert module.key == "documents" and {b.key for b in sheet_backends.BACKENDS} <= {b.key for b in module.backends}
    assert module.backend("sqlite").title == "SQLite database tables (Pocket Access)"
    assert module.backend("excel").title == "Microsoft Excel workbooks on OneDrive"
    assert module.backend("sheets").title == "Google Sheets"
    assert module.backend("numbers").title == "Apple Numbers files in a folder"
    tables = module.device_store(client, Account("a", "documents", "sqlite", (("db_prefix", "PA_"),)), context(tmp_path))
    assert isinstance(tables, DeviceTableStore) and tables.list() == ()
    for backend in ("excel", "sheets", "numbers"):
        folder = module.device_store(client, Account("a", "documents", backend, (("folder", "\\Sheets"),)), context(tmp_path))
        assert isinstance(folder, DeviceFolderStore) and folder.folder == "\\Sheets"


def test_sqlite_tables_sync_with_device_databases_end_to_end(client, device, tmp_path):
    local = DeviceTableStore(client, snapshot_dir=tmp_path / "snapshots")
    remote = SqliteTableStore(tmp_path / "data.sqlite")
    remote.create(Document("Expenses", "Item,Cost\nTea,3\nMilk,1.5\n", "table"))
    state = SyncState()

    first = plan(local.list(), remote.list(), state)
    assert sorted(a.kind for a in first.actions) == ["create_local", "create_remote"]
    state, result = apply(first, local, remote, state)
    assert not result.errors and result.counts == {"create_local": 1, "create_remote": 1}
    assert {i.id: i.record.text for i in remote.list()} == {"Expenses": "Item,Cost\nTea,3\nMilk,1.5\n",
                                                            "Inventory": "Part,Count\nBolt,40\n"}
    with sqlite3.connect(str(tmp_path / "data.sqlite")) as conn:
        assert conn.execute('SELECT "Part", "Count" FROM "Inventory"').fetchall() == [("Bolt", 40)]
    expenses = list(device.db.find("Expenses").records.values())
    assert expenses[0] == (PropVal.string(SCHEMA_PROP, "Item,Cost"),)
    assert [(p.kind, p.value) for p in expenses[1]] == [(CEVT_LPWSTR, "Tea"), (CEVT_I4, 3)]
    assert plan(local.list(), remote.list(), state).is_empty  # both sides fingerprint the same

    remote.update("Expenses", Document("Expenses", "Item,Cost\nTea,3\nMilk,1.5\nBread,2\n", "table"))
    second = plan(local.list(), remote.list(), state)
    assert [(a.kind, a.local_id) for a in second.actions] == [("update_local", "Expenses")]
    state, result = apply(second, local, remote, state)
    assert not result.errors and len(device.db.find("Expenses").records) == 4
    assert list((tmp_path / "snapshots").glob("Expenses.*.json"))
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    assert plan(local.list(), remote.list(), state).is_empty

    inventory = device.db.find("Inventory")
    inventory.records[next(iter(k for k in inventory.records if k != list(inventory.records)[0]))] = (
        PropVal.string(1, "Bolt"), PropVal.i4(2, 41))
    third = plan(local.list(), remote.list(), state, Options(prefer=Prefer.LOCAL))
    assert [a.kind for a in third.actions] == ["update_remote"]
    state, result = apply(third, local, remote, state)
    assert not result.errors and next(i for i in remote.list() if i.id == "Inventory").record.text == "Part,Count\nBolt,41\n"

    device.db.databases.pop(inventory.oid)
    fourth = plan(local.list(), remote.list(), state)
    assert [a.kind for a in fourth.actions] == ["delete_remote"]
    state, result = apply(fourth, local, remote, state)
    assert not result.errors and [i.id for i in remote.list()] == ["Expenses"]
    assert {(l.local_id, l.remote_id) for l in state.links} == {("Expenses", "Expenses")}
    assert plan(local.list(), remote.list(), state).is_empty
