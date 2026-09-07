import json
from datetime import date

import pytest

from jornada.pim.models import Address, Contact
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.contacts import apple
from jornada.sync.contacts.apple import (CREATE_SCRIPT, DELETE_SCRIPT, LIST_SCRIPT, UPDATE_SCRIPT, AppleContactsStore,
                                         address_kind, address_label, fields_of, label_key, person_of, phone_kind,
                                         phone_label)
from jornada.sync.registry import BuildContext
from jornada.webapi.applescript import fake_runner

PID = "1A2B3C4D-0000-1111-2222-333344445555:ABPerson"
STAMP = "2026-09-06T10:00:00.000Z"
CONTACT = Contact(
    first_name="Ada", last_name="Lovelace", middle_name="King", title="Countess", suffix="PhD", company="Analytical",
    job_title="Mathematician", department="R&D", emails=("ada@x.org", "ada@home.org"),
    phones=(("mobile", "5"), ("work", "1"), ("home", "3"), ("work2", "2"), ("work_fax", "6")),
    addresses=(Address("home", "1 Rd", "Town", "ST", "123", "UK"), Address("other", "Campus")),
    birthday=date(1815, 12, 10), web_page="https://x.org", notes="first\nsecond", categories=("ignored",),
    spouse="ignored too",
)


def entry(**overrides):
    base = {
        "id": PID, "firstName": "Ada", "middleName": "King", "lastName": "Lovelace", "title": "Countess",
        "suffix": "PhD", "organization": "Analytical", "jobTitle": "Mathematician", "department": "R&D",
        "birthday": "1815-12-10", "note": "first\r\nsecond", "modified": STAMP,
        "emails": [{"label": "_$!<Work>!$_", "value": "ada@x.org"}, {"label": "_$!<Home>!$_", "value": "ada@home.org"}],
        "phones": [{"label": "_$!<Mobile>!$_", "value": "5"}, {"label": "_$!<Work>!$_", "value": "1"},
                   {"label": "_$!<Main>!$_", "value": "2"}, {"label": "_$!<Home>!$_", "value": "3"},
                   {"label": "_$!<iPhone>!$_", "value": "55"}, {"label": "_$!<WorkFAX>!$_", "value": "6"},
                   {"label": "_$!<HomeFAX>!$_", "value": "7"}, {"label": "_$!<Pager>!$_", "value": "8"},
                   {"label": "_$!<Car>!$_", "value": "9"}, {"label": "Radio", "value": "10"},
                   {"label": "assistant", "value": "11"}, {"label": "School", "value": ""}],
        "addresses": [{"label": "_$!<Home>!$_", "street": "1 Rd", "city": "Town", "state": "ST", "zip": "123", "country": "UK"},
                      {"label": "_$!<Work>!$_", "street": "HQ", "city": "", "state": "", "zip": "", "country": ""},
                      {"label": "School", "street": "Campus", "city": "", "state": "", "zip": "", "country": ""}],
        "url": "https://x.org",
    }
    return {**base, **overrides}


# -- labels ⇄ kinds ------------------------------------------------------------------
def test_label_mapping_both_ways():
    assert label_key("_$!<WorkFAX>!$_") == "workfax" and label_key("Radio") == "radio" and label_key(None) == ""
    assert [phone_kind(label) for label in ("_$!<Work>!$_", "_$!<Main>!$_", "_$!<Home>!$_", "_$!<Mobile>!$_",
                                            "_$!<iPhone>!$_", "_$!<WorkFAX>!$_", "_$!<HomeFAX>!$_", "_$!<Pager>!$_",
                                            "_$!<Car>!$_", "Radio", "Assistant", "School", "")] == [
        "work", "work", "home", "mobile", "mobile", "work_fax", "home_fax", "pager", "car", "radio", "assistant",
        "work", "work"]
    assert [phone_label(kind) for kind in ("work", "work2", "home", "home2", "mobile", "work_fax", "home_fax", "pager",
                                           "car", "radio", "assistant", "bogus")] == [
        "_$!<Work>!$_", "_$!<Work>!$_", "_$!<Home>!$_", "_$!<Home>!$_", "_$!<Mobile>!$_", "_$!<WorkFAX>!$_",
        "_$!<HomeFAX>!$_", "_$!<Pager>!$_", "_$!<Car>!$_", "Radio", "Assistant", "_$!<Work>!$_"]
    assert [address_kind(label) for label in ("_$!<Home>!$_", "_$!<Work>!$_", "School", None)] == ["home", "work", "other", "other"]
    assert [address_label(kind) for kind in ("home", "work", "other", "bogus")] == [
        "_$!<Home>!$_", "_$!<Work>!$_", "_$!<Other>!$_", "_$!<Other>!$_"]


# -- JSON ⇄ Contact ---------------------------------------------------------------------
def test_person_of_maps_labels_slots_and_dates():
    contact = person_of(entry())
    assert contact == Contact(
        first_name="Ada", last_name="Lovelace", middle_name="King", title="Countess", suffix="PhD",
        full_name="Ada King Lovelace", company="Analytical",
        job_title="Mathematician", department="R&D", emails=("ada@x.org", "ada@home.org"),
        phones=(("work", "1"), ("work2", "2"), ("home", "3"), ("mobile", "5"), ("mobile", "55"), ("work_fax", "6"),
                ("home_fax", "7"), ("pager", "8"), ("car", "9"), ("radio", "10"), ("assistant", "11")),
        addresses=(Address("home", "1 Rd", "Town", "ST", "123", "UK"), Address("work", "HQ"), Address("other", "Campus")),
        birthday=date(1815, 12, 10), web_page="https://x.org", notes="first\nsecond", uid=PID,
    )
    sparse = person_of({"id": "p2", "firstName": None, "birthday": "", "emails": "nope", "phones": [1]})
    assert sparse == Contact(uid="p2")
    assert person_of({"id": "p3", "organization": "ACME"}).full_name == "ACME"


def test_fields_of_is_the_json_the_scripts_apply():
    assert fields_of(CONTACT) == {
        "firstName": "Ada", "middleName": "King", "lastName": "Lovelace", "title": "Countess", "suffix": "PhD",
        "organization": "Analytical", "jobTitle": "Mathematician", "department": "R&D", "note": "first\nsecond",
        "birthday": "1815-12-10",
        "emails": [{"label": "_$!<Other>!$_", "value": "ada@x.org"}, {"label": "_$!<Other>!$_", "value": "ada@home.org"}],
        "phones": [{"label": "_$!<Work>!$_", "value": "1"}, {"label": "_$!<Work>!$_", "value": "2"},
                   {"label": "_$!<Home>!$_", "value": "3"}, {"label": "_$!<Mobile>!$_", "value": "5"},
                   {"label": "_$!<WorkFAX>!$_", "value": "6"}],
        "addresses": [{"label": "_$!<Home>!$_", "street": "1 Rd", "city": "Town", "state": "ST", "zip": "123", "country": "UK"},
                      {"label": "_$!<Other>!$_", "street": "Campus", "city": "", "state": "", "zip": "", "country": ""}],
        "url": "https://x.org", "urlLabel": "_$!<HomePage>!$_",
    }
    empty = fields_of(Contact())
    assert empty["birthday"] == "" and empty["emails"] == [] and empty["phones"] == [] and empty["url"] == ""


# -- the store ---------------------------------------------------------------------------
def test_list_sends_the_group_and_maps_json_to_items():
    calls = []
    runner = fake_runner([[entry(), entry(id="p2", firstName="Solo", modified="", emails=[], phones=[], addresses=[])]], calls)
    store = AppleContactsStore(group="Handheld", runner=runner)
    first, second = store.list()
    argv, stdin, _timeout = calls[0]
    assert argv[:4] == ("/usr/bin/osascript", "-l", "JavaScript", "-") and argv[4:] == ("Handheld",)
    assert stdin == LIST_SCRIPT and "run(argv)" in stdin
    assert first.id == PID and first.version == STAMP and first.record == person_of(entry())
    assert second.id == "p2" and second.version is None and second.record.first_name == "Solo"
    assert store.name == "apple-contacts" and store.group == "Handheld"
    everyone = AppleContactsStore(group="*", runner=fake_runner([[]], calls))
    assert everyone.list() == () and calls[1][0][4:] == ("*",)


def test_create_update_delete_pass_the_contact_as_one_json_argument():
    calls = []
    runner = fake_runner([{"id": PID, "modified": "m1"}, {"id": PID, "modified": "m2"}, {"id": PID, "deleted": True}], calls)
    store = AppleContactsStore(runner=runner)
    assert store.create(CONTACT) == PID
    argv, stdin, _ = calls[0]
    assert stdin == CREATE_SCRIPT and argv[4] == "Jornada" and json.loads(argv[5]) == fields_of(CONTACT)
    assert store.update(PID, Contact(first_name="Ada")) == "m2"
    argv, stdin, _ = calls[1]
    assert stdin == UPDATE_SCRIPT and argv[4:6] == ("Jornada", PID) and json.loads(argv[6]) == fields_of(Contact(first_name="Ada"))
    store.delete(PID)
    assert calls[2][1] == DELETE_SCRIPT and calls[2][0][4:] == (PID,)
    for script in (LIST_SCRIPT, CREATE_SCRIPT, UPDATE_SCRIPT, DELETE_SCRIPT):
        assert "JSON.stringify" in script and "Lovelace" not in script and "Jornada" not in script
    assert "Contacts.add(person, {to: group})" in CREATE_SCRIPT and "Contacts.save()" in UPDATE_SCRIPT
    assert "groups.push(Contacts.Group({name: name}))" in LIST_SCRIPT     # the group is created when missing


def test_skipped_notes_and_gone_contacts_are_logged():
    lines = []
    runner = fake_runner([{"id": PID, "modified": "m1", "noteSkipped": True}, {"id": PID, "modified": "m1", "noteSkipped": True},
                          {"id": PID, "deleted": False}])
    store = AppleContactsStore(runner=runner, log=lines.append)
    assert store.create(CONTACT) == PID and len(lines) == 1 and "note" in lines[0] and "Ada King Lovelace" in lines[0]
    assert store.update(PID, Contact(first_name="No note")) == "m1" and len(lines) == 1   # nothing to write anyway
    store.delete(PID)
    assert lines[-1] == f"contact {PID} was already gone from Apple Contacts"


def test_failures_become_store_errors():
    runner = fake_runner([RuntimeError("Contacts got an error: not allowed"), {"nope": 1}, [{"firstName": "no id"}], {"id": ""}])
    store = AppleContactsStore(runner=runner)
    with pytest.raises(StoreError) as info:
        store.list()
    assert "not allowed" in str(info.value) and "Apple Contacts" in str(info.value)
    with pytest.raises(StoreError):
        store.list()                        # an object instead of a list
    with pytest.raises(StoreError):
        store.list()                        # an entry without an id
    with pytest.raises(StoreError):
        store.create(Contact(first_name="x"))   # no id for the new person
    with pytest.raises(StoreError):
        store.update("", Contact())
    with pytest.raises(StoreError):
        store.delete("  ")


def test_backend_build_reads_settings_and_uses_the_context_runner(tmp_path):
    calls = []
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           runner=fake_runner([[]], calls))
    store = apple.build(Account("c", "contacts", "apple", (("group", "Handheld"),)), {"ignored": 1}, context)
    assert store.list() == () and calls[0][0][4:] == ("Handheld",)
    assert apple.build(Account("c", "contacts", "apple"), {}, context).group == "Jornada"
    assert apple.BACKEND.key == "apple" and apple.BACKEND.title == "Apple Contacts (Contacts.app)"
    assert apple.BACKEND.missing_settings(Account("c", "contacts", "apple"), {}) == () and apple.BACKEND.login is None
    assert [(s.key, s.required, s.default) for s in apple.BACKEND.settings] == [("group", False, "Jornada")]
