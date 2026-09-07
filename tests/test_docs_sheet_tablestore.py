import json

import pytest

from jornada.cedb import CEVT_FILETIME, CEVT_I4, CEVT_LPWSTR, CEVT_R8, PropVal, Record
from jornada.pim import ids
from jornada.pim.models import Document
from jornada.rapi import RapiClient
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.documents import tablestore
from jornada.sync.documents.tablestore import SCHEMA_PROP, DeviceTableStore, device_table_store
from jornada.sync.registry import BuildContext
from tests.fake_device import FakeRapiServer

CSV = "Name,Qty,Price,When\nWidget,7,1.5,2026-01-02\n,,,\nGadget,,2.5e-05,2026-01-02T10:30:00\n"
CANONICAL = "Name,Qty,Price,When\nWidget,7,1.5,2026-01-02\nGadget,,2.5e-05,2026-01-02T10:30:00\n"


@pytest.fixture
def device():
    server = FakeRapiServer()
    server.db.create(ids.DB_CONTACTS, records=[(PropVal.string(0x3A06, "Ada"),)])
    server.db.create("PA_Legacy", records=[(PropVal.string(0x0010, "Bolt"), PropVal.i4(0x0020, 40)),
                                           (PropVal.string(0x0010, "Nut"),)])
    server.db.create("PA_Typed", records=[(PropVal.string(SCHEMA_PROP, "k"), ), (PropVal.string(1, "v"),)], db_type=5)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as c:
        yield c


def test_create_writes_schema_record_and_typed_properties(client, device, tmp_path):
    store = DeviceTableStore(client, prefix="PA_", snapshot_dir=tmp_path)
    assert store.create(Document("Stock", CSV, "table")) == "Stock"
    db = device.db.find("PA_Stock")
    records = list(db.records.values())
    assert len(records) == 3  # schema + two rows; the empty row is skipped
    assert records[0] == (PropVal.string(SCHEMA_PROP, "Name,Qty,Price,When"),)
    assert [(p.prop_id, p.kind) for p in records[1]] == [(1, CEVT_LPWSTR), (2, CEVT_I4), (3, CEVT_R8), (4, CEVT_FILETIME)]
    assert [(p.prop_id, p.kind) for p in records[2]] == [(1, CEVT_LPWSTR), (3, CEVT_R8), (4, CEVT_FILETIME)]
    assert records[1][1].value == 7 and records[1][2].value == 1.5
    assert list(tmp_path.glob("*.json")) == []  # nothing to snapshot before a database exists
    with pytest.raises(StoreError):
        store.create(Document("Stock", CSV, "table"))
    with pytest.raises(StoreError):
        store.create(Document("Bad", "", "table"))


def test_list_decodes_databases_back_to_csv(client, tmp_path):
    store = DeviceTableStore(client, prefix="PA_", snapshot_dir=tmp_path)
    store.create(Document("Stock", CSV, "table"))
    items = {i.id: i for i in store.list()}
    assert sorted(items) == ["Legacy", "Stock", "Typed"]
    assert items["Stock"].record == Document("Stock", CANONICAL, "table") and items["Stock"].version is None
    assert items["Legacy"].record.text == "c0016,c0032\nBolt,40\nNut\n"  # no schema record: generated names
    assert items["Typed"].record.text == "k\nv\n"
    typed_only = DeviceTableStore(client, prefix="PA_", db_type=5, snapshot_dir=tmp_path)
    assert [i.id for i in typed_only.list()] == ["Typed"]
    everything = DeviceTableStore(client, snapshot_dir=tmp_path)
    assert sorted(i.id for i in everything.list()) == ["PA_Legacy", "PA_Stock", "PA_Typed"]  # never Contacts Database


def test_update_rewrites_after_one_snapshot_and_delete_removes(client, device, tmp_path):
    log = []
    store = DeviceTableStore(client, prefix="PA_", snapshot_dir=tmp_path, log=log.append)
    store.create(Document("Stock", CSV, "table"))
    first_oids = list(device.db.find("PA_Stock").records)
    assert store.update("Stock", Document("Stock", "Part,Count\nBolt,40\n\nNut,\n", "table")) is None
    db = device.db.find("PA_Stock")
    assert not set(db.records) & set(first_oids)
    assert list(db.records.values()) == [(PropVal.string(SCHEMA_PROP, "Part,Count"),),
                                         (PropVal.string(1, "Bolt"), PropVal.i4(2, 40)), (PropVal.string(1, "Nut"),)]
    (snapshot,) = tmp_path.glob("PA_Stock.*.json")
    payload = json.loads(snapshot.read_text())
    assert payload["database"] == "PA_Stock" and len(payload["records"]) == 3
    assert payload["records"][0]["props"][0] == {"id": SCHEMA_PROP, "kind": "string", "value": "Name,Qty,Price,When", "flags": 0}
    store.update("Stock", Document("Stock", "Part\nx\n", "table"))
    store.delete("Stock")
    assert device.db.find("PA_Stock") is None and len(list(tmp_path.glob("*.json"))) == 1  # one snapshot per session
    assert any("snapshot of 'PA_Stock'" in line for line in log) and any("deleted database 'PA_Stock'" in line for line in log)
    with pytest.raises(StoreError):
        store.delete("Stock")
    with pytest.raises(StoreError):
        store.update("Stock", Document("Stock", "a\n", "table"))
    store.delete("Legacy")  # snapshots the second database too
    assert len(list(tmp_path.glob("PA_Legacy.*.json"))) == 1
    silent = DeviceTableStore(client, prefix="PA_")  # no snapshot directory: writes still work
    silent.update("Typed", Document("Typed", "k\nw\n", "table"))
    assert list(device.db.find("PA_Typed").records.values())[1] == (PropVal.string(1, "w"),)


def test_pocket_outlook_databases_are_refused_whatever_the_prefix(client, device, tmp_path):
    for prefix in ("", "PA_"):
        store = DeviceTableStore(client, prefix=prefix, snapshot_dir=tmp_path)
        for name in (ids.DB_CONTACTS, ids.DB_APPOINTMENTS, "tasks database"):
            with pytest.raises(StoreError):
                store.create(Document(name, "a\n1\n", "table"))
            with pytest.raises(StoreError):
                store.update(name, Document(name, "a\n1\n", "table"))
            with pytest.raises(StoreError):
                store.delete(name)
    assert device.db.find(ids.DB_CONTACTS).records
    with pytest.raises(StoreError):
        DeviceTableStore(client, prefix="Contacts ", snapshot_dir=tmp_path).delete("Database")
    with pytest.raises(StoreError):
        DeviceTableStore(client, snapshot_dir=tmp_path).create(Document("x" * 33, "a\n1\n", "table"))
    with pytest.raises(StoreError):
        DeviceTableStore(client, snapshot_dir=tmp_path).create(Document("  ", "a\n1\n", "table"))


def test_cell_and_property_codec():
    assert tablestore.cell_prop(1, "") is None
    assert tablestore.cell_prop(1, "2147483647") == PropVal.i4(1, 2147483647)
    assert tablestore.cell_prop(1, "2147483648") == PropVal.string(1, "2147483648")
    assert tablestore.cell_prop(1, "-1.5") == PropVal.r8(1, -1.5)
    assert tablestore.cell_prop(1, "007") == PropVal.string(1, "007")
    assert tablestore.cell_prop(1, "1500-01-01") == PropVal.string(1, "1500-01-01")
    stamp = tablestore.cell_prop(1, "2026-01-02T10:30:00")
    assert stamp.kind == CEVT_FILETIME and tablestore.prop_text(stamp) == "2026-01-02T10:30:00"
    assert tablestore.prop_text(tablestore.cell_prop(1, "2026-01-02")) == "2026-01-02"
    assert tablestore.prop_text(None) == "" and tablestore.prop_text(PropVal(1, CEVT_I4, None, 0x0100)) == ""
    assert tablestore.prop_text(PropVal.boolean(1, True)) == "TRUE" and tablestore.prop_text(PropVal.blob(1, b"\xca\xfe")) == "cafe"
    assert tablestore.prop_text(PropVal.ui2(1, 9)) == "9" and tablestore.prop_text(PropVal.filetime(1, 0)) == ""
    assert tablestore.prop_text(PropVal(1, 0x99, b"raw")) == "b'raw'"
    assert tablestore.row_props(("a", "", "1")) == (PropVal.string(1, "a"), PropVal.i4(3, 1))
    assert tablestore.schema_prop(("a,b", 'q"q')) == PropVal.string(SCHEMA_PROP, '"a,b","q""q"')
    assert tablestore.records_to_csv(()) == ""
    records = (Record(1, (PropVal.string(SCHEMA_PROP, "a,b"),)), Record(2, (PropVal.i4(2, 5), PropVal.string(9, "extra"))))
    assert tablestore.records_to_csv(records) == "a,b,c0009\n,5,extra\n"


def test_device_table_store_reads_account_settings(client, tmp_path):
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    store = device_table_store(client, Account("a", "documents", "sqlite", (("db_prefix", "PA_"), ("db_type", "5"))), context)
    assert isinstance(store, DeviceTableStore) and [i.id for i in store.list()] == ["Typed"]
    store.update("Typed", Document("Typed", "k\nz\n", "table"))
    assert (tmp_path / "snapshots").is_dir() and list((tmp_path / "snapshots").glob("PA_Typed.*.json"))
    plain = device_table_store(client, Account("a", "documents", "sqlite"), context)
    assert sorted(i.id for i in plain.list()) == ["PA_Legacy", "PA_Typed"]
    with pytest.raises(StoreError):
        device_table_store(client, Account("a", "documents", "sqlite", (("db_type", "five"),)), context)
