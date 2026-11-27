from datetime import date

import pytest

from jornada.cedb import PropVal
from jornada.pim import ids, store as pim_store
from jornada.pim.models import Contact
from jornada.pim.store import DeviceStore
from jornada.pim.timeconv import date_to_filetime
from jornada.rapi import RapiClient
from jornada.sync import registry
from jornada.sync.accounts import Account
from jornada.sync.contacts import MODULE, device_store
from jornada.sync.contacts.carddav import CardDavContactsStore
from jornada.sync.engine import apply, plan, refresh_hashes
from jornada.sync.registry import BuildContext
from jornada.sync.state import SyncState
from jornada.webapi import vcard
from tests.fake_dav import PASSWORD, USERNAME, FakeDavServer, Item
from tests.fake_device import FakeRapiServer

VCARD_TYPE = "text/vcard; charset=utf-8"
BOB = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:bob\r\nN:Smith;Bob;;;\r\nFN:Bob Smith\r\nTEL;TYPE=CELL:+1 555\r\n"
       "EMAIL;TYPE=INTERNET:bob@x.org\r\nEND:VCARD\r\n")


@pytest.fixture
def device():
    server = FakeRapiServer()
    server.db.create(ids.DB_CONTACTS, records=[
        (PropVal.string(ids.CONTACT_FIRST_NAME, "Ada"), PropVal.string(ids.CONTACT_LAST_NAME, "Lovelace"),
         PropVal.string(ids.CONTACT_FULL_NAME, "Ada Lovelace"), PropVal.string(ids.CONTACT_EMAIL, "ada@x.org"),
         PropVal.string(ids.CONTACT_WORK_TEL, "1"), PropVal.string(ids.CONTACT_WORK2_TEL, "2"),
         PropVal.string(ids.CONTACT_MOBILE_TEL, "5"),
         PropVal.filetime(ids.CONTACT_BIRTHDAY, date_to_filetime(date(1815, 12, 10)))),
        (PropVal.string(ids.CONTACT_FIRST_NAME, "Jane"), PropVal.string(ids.CONTACT_LAST_NAME, "Doe"),
         PropVal.string(ids.CONTACT_FULL_NAME, "Jane Doe")),       # the same person as the server's c1.vcf: paired
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


@pytest.fixture
def dav_server():
    srv = FakeDavServer()
    srv.items["/card/default/bob.vcf"] = Item('"etag-bob"', BOB, VCARD_TYPE)      # only on the server
    srv.items["/card/default/junk.vcf"] = Item('"etag-junk"', "BEGIN:VCARD\r\nno colon\r\nEND:VCARD\r\n", VCARD_TYPE)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def context(tmp_path, lines=None):
    log = lines.append if lines is not None else (lambda _line: None)
    return BuildContext(log=log, sync_dir=tmp_path / "sync", save_secrets=lambda _changes: None)


def device_record(db, first_name):
    return next((oid, props) for oid, props in db.records.items()
                if any(p.prop_id == ids.CONTACT_FIRST_NAME and p.value == first_name for p in props))


def phones_of(props):
    by_id = {p.prop_id: p.value for p in props}
    return {kind: by_id[prop_id] for kind, prop_id in ids.CONTACT_PHONE_SLOTS.items() if prop_id in by_id}


def test_module_spec_and_registry():
    assert MODULE.key == "contacts" and MODULE.title == "Contacts (Pocket Outlook Contacts)" and not MODULE.is_bridge
    assert [b.key for b in MODULE.backends] == ["apple", "google", "m365", "carddav"]
    assert MODULE.settings == () and MODULE.device_store is device_store and MODULE.notes
    assert registry.load_modules({"contacts": "jornada.sync.contacts"})["contacts"] is MODULE
    assert [b.title for b in MODULE.backends] == ["Apple Contacts (Contacts.app)", "Google Contacts", "Microsoft 365 contacts",
                                                  "CardDAV (Fastmail, Nextcloud, iCloud, …)"]
    for backend in MODULE.backends:
        assert backend.title and backend.notes and all(spec.help for spec in backend.settings)
    assert [b.login is not None for b in MODULE.backends] == [False, True, True, False]
    assert MODULE.backend("carddav").missing_settings(Account("a", "contacts", "carddav"), {}) == ("url", "username", "password")


def test_device_store_is_the_contacts_database(client, device, tmp_path, monkeypatch):
    monkeypatch.setattr(pim_store, "DEFAULT_SNAPSHOT_DIR", tmp_path / "snapshots")
    store = device_store(client, Account("hand", "contacts", "carddav"), context(tmp_path))
    assert isinstance(store, DeviceStore) and store.database == ids.DB_CONTACTS
    ada, jane = store.list()
    assert ada.record == Contact(first_name="Ada", last_name="Lovelace", full_name="Ada Lovelace", emails=("ada@x.org",),
                                 phones=(("work", "1"), ("work2", "2"), ("mobile", "5")), birthday=date(1815, 12, 10))
    assert jane.record.display_name() == "Jane Doe" and ada.record.match_key() == "contact|ada lovelace|ada@x.org"


def test_end_to_end_between_the_device_and_a_carddav_server(client, device, dav_server, tmp_path, monkeypatch):
    monkeypatch.setattr(pim_store, "DEFAULT_SNAPSHOT_DIR", tmp_path / "snapshots")
    account = Account("hand", "contacts", "carddav", (("url", dav_server.base_url), ("username", USERNAME)))
    ctx = context(tmp_path)
    local = MODULE.device_store(client, account, ctx)
    remote = MODULE.backend(account.backend).build(account, {"password": PASSWORD}, ctx)
    assert isinstance(remote, CardDavContactsStore)
    db = device.db.find(ids.DB_CONTACTS)

    first = plan(local.list(), remote.list(), SyncState())
    assert sorted(a.kind for a in first.actions) == ["create_local", "create_remote", "link", "skip"]
    assert next(a for a in first.actions if a.kind == "link").remote_id == "/card/default/c1.vcf"
    assert next(a for a in first.actions if a.kind == "skip").reason.startswith("unreadable")
    state, result = apply(first, local, remote, SyncState())
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    assert len(state.links) == 3 and list((tmp_path / "snapshots").glob("Contacts_Database.*.json"))
    ada_href = next(l.remote_id for l in state.links if l.remote_id not in ("/card/default/c1.vcf", "/card/default/bob.vcf"))
    assert ada_href.startswith("/card/default/") and ada_href.endswith(".vcf") and "junk" not in ada_href
    ada_card = vcard.parse(dav_server.items[ada_href].data)[0]
    assert [line for line in dav_server.items[ada_href].data.split("\r\n") if line.startswith("TEL")] == [
        "TEL;TYPE=WORK,VOICE:1", "TEL;TYPE=WORK,VOICE:2", "TEL;TYPE=CELL:5"]
    assert ada_card.value("BDAY") == "18151210" and ada_card.value("EMAIL") == "ada@x.org"
    bob_oid, bob_props = device_record(db, "Bob")
    assert phones_of(bob_props) == {"mobile": "+1 555"} and any(p.value == "bob@x.org" for p in bob_props)
    remote_ada = next(i for i in remote.list() if i.id == ada_href).record
    assert remote_ada.phones == (("work", "1"), ("work2", "2"), ("mobile", "5"))       # work2 survives the round trip
    assert plan(local.list(), remote.list(), state).is_empty

    ada_oid, ada_props = device_record(db, "Ada")                                    # edited on the device
    db.records[ada_oid] = ada_props + (PropVal.string(ids.CONTACT_HOME_TEL, "3"),)
    second = plan(local.list(), remote.list(), state)
    assert [(a.kind, a.remote_id) for a in second.actions if a.kind != "skip"] == [("update_remote", ada_href)]
    state, result = apply(second, local, remote, state)
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    rewritten = dav_server.items[ada_href].data
    assert "TEL;TYPE=HOME,VOICE:3" in rewritten and "TEL;TYPE=WORK,VOICE:2" in rewritten and "UID:" in rewritten
    assert next(i for i in local.list() if i.id == str(ada_oid)).record.phones == (
        ("work", "1"), ("work2", "2"), ("home", "3"), ("mobile", "5"))
    assert plan(local.list(), remote.list(), state).is_empty

    bob_card = BOB.replace("FN:Bob Smith", "FN:Bob Smith\r\nORG:Widgets Ltd").replace("TEL;TYPE=CELL:+1 555", "TEL;TYPE=CELL:+1 555\r\nTEL;TYPE=WORK:+1 666")
    dav_server.items["/card/default/bob.vcf"] = Item('"etag-bob-2"', bob_card, VCARD_TYPE)   # edited on the server
    del dav_server.items["/card/default/c1.vcf"]                                             # deleted on the server
    third = plan(local.list(), remote.list(), state)
    assert sorted(a.kind for a in third.actions) == ["delete_local", "skip", "update_local"]
    state, result = apply(third, local, remote, state)
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    _oid, bob_props = device_record(db, "Bob")
    assert phones_of(bob_props) == {"work": "+1 666", "mobile": "+1 555"}
    assert any(p.prop_id == ids.CONTACT_COMPANY and p.value == "Widgets Ltd" for p in bob_props)
    assert not any(p.value == "Jane" for props in db.records.values() for p in props)
    assert len(state.links) == 2 and plan(local.list(), remote.list(), state).is_empty

    db.records[bob_oid] = tuple(p for p in db.records[bob_oid] if p.prop_id != ids.CONTACT_FIRST_NAME) + (
        PropVal.string(ids.CONTACT_FIRST_NAME, "Robert"),)                             # both sides edited: remote wins
    dav_server.items["/card/default/bob.vcf"] = Item('"etag-bob-3"', bob_card.replace("N:Smith;Bob", "N:Smyth;Bob"), VCARD_TYPE)
    fourth = plan(local.list(), remote.list(), state)
    assert [(a.kind, a.reason) for a in fourth.actions if a.kind != "skip"] == [
        ("update_local", "changed on both sides; remote wins")]
    state, result = apply(fourth, local, remote, state)
    assert not result.errors
    _oid, bob_props = device_record(db, "Bob")
    assert any(p.prop_id == ids.CONTACT_LAST_NAME and p.value == "Smyth" for p in bob_props)
