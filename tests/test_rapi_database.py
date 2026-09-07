import pytest

from jornada.cedb import CEVT_LPWSTR, PropVal
from jornada.constants import CEDB_SEEK_BEGINNING, CEDB_SEEK_CEOID, CEDB_SEEK_CURRENT, CEDB_SEEK_END, FAD_LISTING, FAD_SORT_SPECS
from jornada.rapi import RapiClient, RapiError
from tests.fake_device import FakeRapiServer

SUBJECT, NOTES, START = 0x0037, 0x0017, 0x420D


@pytest.fixture
def device():
    server = FakeRapiServer()
    server.db.create("Appointments Database", db_type=0x100, records=[
        (PropVal.string(SUBJECT, "Dentist"), PropVal.filetime(START, 0x01D9E0F09B2C3D4E), PropVal.blob(NOTES, b"bring card\r\n")),
        (PropVal.string(SUBJECT, "Lunch"), PropVal.i2(0x4223, 0)),
    ])
    server.db.create("Contacts Database", db_type=0x200)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as c:
        yield c


def test_find_all_databases_lists_names_counts_and_types(client):
    listing = client.find_all_databases()
    assert [(d.name, d.num_records, d.db_type) for d in listing] == [
        ("Appointments Database", 2, 0x100), ("Contacts Database", 0, 0x200)]
    assert listing[0].oid != listing[1].oid and listing[0].last_modified is not None
    assert [d.name for d in client.find_all_databases(db_type=0x200)] == ["Contacts Database"]
    with_specs = client.find_all_databases(flags=FAD_LISTING | FAD_SORT_SPECS)
    assert len(with_specs[0].sort_specs) == 4
    assert client.find_database("contacts database").num_records == 0
    assert client.find_database("Inbox") is None


def test_open_iterate_and_close(client):
    info = client.find_database("Appointments Database")
    handle = client.open_database(info.oid)
    records = list(client.iter_records(handle))
    assert [r.value(SUBJECT) for r in records] == ["Dentist", "Lunch"]
    assert records[0].value(NOTES) == b"bring card\r\n" and records[0].value(START) == 0x01D9E0F09B2C3D4E
    assert client.read_record(handle) is None  # still at the end
    client.close_handle(handle)
    with pytest.raises(RapiError):
        client.read_record(handle)


def test_open_unknown_database_raises(client):
    with pytest.raises(RapiError):
        client.open_database(0xDEAD)


def test_write_new_update_and_delete_record(client, device):
    info = client.find_database("Contacts Database")
    handle = client.open_database(info.oid)
    oid = client.write_record(handle, [PropVal.string(0x3A06, "Ada"), PropVal.string(0x3A11, "Lovelace")])
    assert oid != 0
    db = device.db.find("Contacts Database")
    assert [p.value for p in db.records[oid]] == ["Ada", "Lovelace"]
    # update: replace one property, add one, delete one
    same = client.write_record(handle, [PropVal.string(0x3A11, "Byron"), PropVal.string(0x4083, "ada@x.org"),
                                        PropVal.deleted(0x3A06, CEVT_LPWSTR)], oid=oid)
    assert same == oid
    assert {p.prop_id: p.value for p in db.records[oid]} == {0x3A11: "Byron", 0x4083: "ada@x.org"}
    with pytest.raises(RapiError):
        client.write_record(handle, [PropVal.string(1, "x")], oid=0x7777)
    client.delete_record(handle, oid)
    assert oid not in db.records
    with pytest.raises(RapiError):
        client.delete_record(handle, oid)
    client.close_handle(handle)


def test_seek(client):
    info = client.find_database("Appointments Database")
    handle = client.open_database(info.oid)
    first, index = client.seek_database(handle, CEDB_SEEK_BEGINNING)
    assert index == 0 and client.read_record(handle).oid == first
    last, index = client.seek_database(handle, CEDB_SEEK_END)
    assert index == 1 and client.read_record(handle).value(SUBJECT) == "Lunch"
    assert client.seek_database(handle, CEDB_SEEK_CURRENT, -2)[1] == 0
    assert client.seek_database(handle, CEDB_SEEK_CEOID, last)[0] == last
    assert client.seek_database(handle, CEDB_SEEK_BEGINNING, 99)[0] == 0
    client.close_handle(handle)


def test_create_and_delete_database(client, device):
    oid = client.create_database("Tasks Database", db_type=0x300, sort_specs=[(0x00370000 | 31, 0)])
    assert device.db.databases[oid].name == "Tasks Database"
    assert device.db.databases[oid].sort_specs == ((0x0037001F, 0),)
    with pytest.raises(RapiError):
        client.create_database("Tasks Database")
    with pytest.raises(ValueError):
        client.create_database("x", sort_specs=[(1, 0)] * 5)
    client.delete_database(oid)
    assert oid not in device.db.databases
    with pytest.raises(RapiError):
        client.delete_database(oid)


def test_read_all_records_convenience(client):
    info, records = client.read_all_records("Appointments Database")
    assert info.name == "Appointments Database" and len(records) == 2
    with pytest.raises(RapiError):
        client.read_all_records("Nope")
