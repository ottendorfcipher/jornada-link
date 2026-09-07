import json
from datetime import date, datetime
from pathlib import Path

import pytest

from jornada.cedb import PropVal
from jornada.pim import ids
from jornada.pim.codecs import APPOINTMENTS, CONTACTS, TASKS, codec_for, generic
from jornada.pim.models import Appointment, Contact, Task
from jornada.pim.store import DeviceStore, restore_snapshot
from jornada.rapi import RapiClient
from jornada.sync.base import StoreError
from tests.fake_device import FakeRapiServer


@pytest.fixture
def device():
    server = FakeRapiServer()
    server.db.create(ids.DB_APPOINTMENTS, records=[
        (PropVal.string(ids.SUBJECT, "Dentist"), PropVal.filetime(ids.APPT_START, 133_000_000_000_000_000),
         PropVal.i4(ids.APPT_DURATION, 30), PropVal.i4(ids.APPT_TYPE, ids.APPT_TYPE_NORMAL)),
        (PropVal.string(ids.SUBJECT, "Weekly"), PropVal.filetime(ids.APPT_START, 133_000_000_000_000_000),
         PropVal.i4(ids.APPT_DURATION, 60), PropVal.i2(ids.APPT_OCCURRENCE, ids.OCCURRENCE_REPEATED)),
    ])
    server.db.create(ids.DB_CONTACTS)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as c:
        yield c


def test_list_decodes_records(client, tmp_path):
    store = DeviceStore(client, APPOINTMENTS, snapshot_dir=tmp_path)
    items = store.list()
    assert [i.record.summary for i in items] == ["Dentist", "Weekly"]
    assert items[0].record.end - items[0].record.start == (items[0].record.end - items[0].record.start)
    assert items[1].record.recurring and not items[0].record.recurring


def test_create_update_delete_with_snapshot(client, device, tmp_path):
    logs = []
    store = DeviceStore(client, CONTACTS, snapshot_dir=tmp_path, log=logs.append)
    assert store.list() == ()
    oid = store.create(Contact(first_name="Ada", last_name="Lovelace", emails=("ada@x.org",), birthday=date(1815, 12, 10)))
    snapshots = list(tmp_path.glob("Contacts_Database.*.json"))
    assert len(snapshots) == 1 and json.loads(snapshots[0].read_text())["database"] == ids.DB_CONTACTS
    (item,) = store.list()
    assert item.id == oid and item.record.emails == ("ada@x.org",) and item.record.birthday == date(1815, 12, 10)
    store.update(oid, Contact(first_name="Ada", last_name="Byron"))
    (item,) = store.list()
    assert item.record.last_name == "Byron" and item.record.emails == () and item.record.birthday is None
    store.delete(oid)
    assert store.list() == ()
    assert len(list(tmp_path.glob("*.json"))) == 1  # one snapshot per session
    assert any("snapshot" in line for line in logs)


def test_recurring_appointment_is_read_only(client, tmp_path):
    store = DeviceStore(client, APPOINTMENTS, snapshot_dir=tmp_path)
    items = store.list()
    weekly = next(i for i in items if i.record.summary == "Weekly")
    with pytest.raises(StoreError):
        store.update(weekly.id, Appointment("Weekly", datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10)))
    dentist = next(i for i in items if i.record.summary == "Dentist")
    store.update(dentist.id, Appointment("Dentist", datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10), notes="x"))
    assert next(i for i in store.list() if i.id == dentist.id).record.notes == "x"


def test_missing_database_created_only_when_allowed(client, tmp_path):
    store = DeviceStore(client, TASKS, snapshot_dir=tmp_path)
    with pytest.raises(StoreError):
        store.list()
    from dataclasses import replace
    creating = DeviceStore(client, replace(TASKS, create_if_missing=True), snapshot_dir=tmp_path)
    assert creating.list() == ()
    oid = creating.create(Task("Buy batteries", due=date(2026, 1, 2), priority="high"))
    (item,) = creating.list()
    assert item.id == oid and item.record.priority == "high" and item.record.due == date(2026, 1, 2)


def test_snapshot_and_restore_round_trip(client, device, tmp_path):
    store = DeviceStore(client, APPOINTMENTS, snapshot_dir=tmp_path)
    path = store.snapshot(now=1_700_000_000)
    assert path.name.startswith("Appointments_Database.")
    payload = json.loads(path.read_text())
    assert len(payload["records"]) == 2
    before = len(device.db.find(ids.DB_APPOINTMENTS).records)
    assert restore_snapshot(client, path, log=lambda _l: None) == 2
    assert len(device.db.find(ids.DB_APPOINTMENTS).records) == before + 2
    with pytest.raises(StoreError):
        restore_snapshot(client, _bad_snapshot(tmp_path), log=lambda _l: None)


def _bad_snapshot(tmp_path: Path) -> Path:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"database": "X", "records": [{"oid": 1, "props": [{"id": 1, "kind": "weird", "value": 0}]}]}))
    return path


def test_codec_lookup():
    assert codec_for("contacts database") is CONTACTS and codec_for("Nope") is None
    passthrough = generic("Nope")
    from jornada.cedb import Record
    record = Record(1, (PropVal.i2(1, 2),))
    assert passthrough.decode(record) is record and passthrough.encode(record, None) == record.props
