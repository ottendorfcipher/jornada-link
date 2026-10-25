from datetime import date

import pytest

from jornada.pim.models import Address, Contact
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.contacts import carddav
from jornada.sync.contacts.carddav import (CardDavContactsStore, UnreadableCard, choose_addressbook, is_group_card,
                                           object_path, read_card)
from jornada.sync.contacts.vcardmap import from_vcard
from jornada.sync.registry import BuildContext
from jornada.webapi import dav, vcard
from tests.fake_dav import PASSWORD, USERNAME, FakeDavServer, Item

VCARD_TYPE = "text/vcard; charset=utf-8"


def crlf(text):
    return text.replace("\n", "\r\n")


ADA = crlf("""BEGIN:VCARD
VERSION:3.0
UID:ada-uid
N:Lovelace;Ada;King;;
FN:Ada King Lovelace
ORG:Analytical;R&D
EMAIL;TYPE=INTERNET,PREF:ada@x.org
TEL;TYPE=WORK,VOICE:1
TEL;TYPE=WORK,VOICE:2
TEL;TYPE=CELL:5
ADR;TYPE=HOME:;;1 Rd;Town;ST;123;UK
BDAY:18151210
CATEGORIES:Science
PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQ
END:VCARD
""")
GROUP = crlf("""BEGIN:VCARD
VERSION:3.0
UID:group-uid
N:Friends;;;;
FN:Friends
X-ADDRESSBOOKSERVER-KIND:group
X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:c1
END:VCARD
""")
BROKEN = crlf("BEGIN:VCARD\nVERSION:3.0\nno colon here\nEND:VCARD\n")


@pytest.fixture
def server():
    srv = FakeDavServer()
    srv.items["/card/default/ada.vcf"] = Item('"etag-ada"', ADA, VCARD_TYPE)
    srv.items["/card/default/group.vcf"] = Item('"etag-group"', GROUP, VCARD_TYPE)
    srv.items["/card/default/broken.vcf"] = Item('"etag-broken"', BROKEN, VCARD_TYPE)
    srv.items["/card/default/empty.vcf"] = Item('"etag-empty"', "", VCARD_TYPE)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def store(server):
    client = dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5)
    uids = iter(["new-1", "new-2", "new-3"])
    return CardDavContactsStore(client, addressbook="Contacts", uid_factory=lambda: next(uids))


def puts(server):
    return [(path, headers) for verb, path, headers in server.requests if verb == "PUT"]


# -- helpers ---------------------------------------------------------------------------------
def test_addressbook_choice_and_paths():
    first = dav.Collection("https://h/card/a/", "Personal", None, (), "addressbook")
    second = dav.Collection("https://h/card/b/", "Work", None, (), "addressbook")
    assert choose_addressbook((first, second), "") is first
    assert choose_addressbook((first, second), "work") is second
    assert choose_addressbook((first, second), "/card/b/") is second
    assert choose_addressbook((first, second), "https://h/card/a") is first
    with pytest.raises(StoreError) as info:
        choose_addressbook((first, second), "Nope")
    assert "Personal, Work" in str(info.value)
    with pytest.raises(StoreError):
        choose_addressbook((), "")
    assert object_path("https://h/card/x.vcf") == "/card/x.vcf" and object_path("/card/y.vcf") == "/card/y.vcf"
    assert is_group_card(vcard.parse(GROUP)[0]) and not is_group_card(vcard.parse(ADA)[0])
    assert is_group_card(vcard.parse("BEGIN:VCARD\nKIND:Group\nEND:VCARD\n")[0])
    assert read_card(GROUP, '"e"') is None and read_card(ADA, '"e"')[1].uid == "ada-uid"
    for junk in (BROKEN, ""):
        with pytest.raises(UnreadableCard):
            read_card(junk, None)


# -- the store against the fake server -------------------------------------------------------
def test_list_maps_cards_reports_unreadable_ones_and_leaves_groups_out(server, store):
    lines = []
    store._log = lines.append
    items = store.list()
    assert [(i.id, i.version) for i in items] == [
        ("/card/default/ada.vcf", '"etag-ada"'), ("/card/default/broken.vcf", '"etag-broken"'),
        ("/card/default/c1.vcf", '"etag-c1"'), ("/card/default/empty.vcf", '"etag-empty"')]
    ada, broken, jane, empty = items
    assert ada.record == Contact(first_name="Ada", last_name="Lovelace", middle_name="King", full_name="Ada King Lovelace",
                                 company="Analytical", department="R&D", emails=("ada@x.org",),
                                 phones=(("work", "1"), ("work2", "2"), ("mobile", "5")),
                                 addresses=(Address("home", "1 Rd", "Town", "ST", "123", "UK"),), birthday=date(1815, 12, 10),
                                 categories=("Science",), uid="ada-uid")
    assert jane.record == Contact(first_name="Jane", last_name="Doe", full_name="Jane Doe", uid="c1")
    assert broken.unreadable and broken.problem and not ada.unreadable and ada.problem is None
    assert empty.unreadable and empty.problem == "the resource holds no vCard"
    assert sorted(line.split(" ")[0] for line in lines if "left alone" in line) == ["/card/default/broken.vcf", "/card/default/empty.vcf"]
    assert [line for line in lines if line.startswith("skipping")] == ["skipping /card/default/group.vcf: it is a group, not a person"]
    report = [(p, h) for verb, p, h in server.requests if verb == "REPORT"][-1]
    assert report[0] == "/card/default/" and report[1].get("Depth") == "1"
    assert store.collection().href == server.base_url + "card/default/" and store.name == "carddav"
    default = CardDavContactsStore(dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5))
    assert default.collection().display_name == "Contacts"


def test_create_update_delete_lifecycle(server, store):
    store.list()
    bob = Contact(first_name="Bob", last_name="Smith", emails=("bob@x",), phones=(("work", "1"), ("work", "2")))
    item_id = store.create(bob)
    assert item_id == "/card/default/new-1.vcf" and item_id in server.items
    path, headers = puts(server)[-1]
    assert path == item_id and headers.get("If-None-Match") == "*" and headers.get("Content-Type") == "text/vcard; charset=utf-8"
    stored = vcard.parse(server.items[item_id].data)[0]
    assert stored.value("UID") == "new-1" and from_vcard(stored) == Contact(first_name="Bob", last_name="Smith", full_name="Bob Smith",
                                                                               emails=("bob@x",), phones=(("work", "1"), ("work2", "2")),
                                                                               uid="new-1")
    (created,) = [i for i in store.list() if i.id == item_id]
    assert created.version == server.items[item_id].etag

    new_etag = store.update("/card/default/ada.vcf", Contact(first_name="Ada", last_name="Byron", emails=("ada@x.org",)))
    path, headers = puts(server)[-1]
    assert path == "/card/default/ada.vcf" and headers.get("If-Match") == '"etag-ada"' and new_etag == server.items[path].etag
    updated = vcard.parse(server.items[path].data)[0]
    assert updated.value("UID") == "ada-uid" and updated.value("N") == "Byron;Ada;;;" and updated.get("PHOTO") is None

    store.delete("/card/default/c1.vcf")
    assert "/card/default/c1.vcf" not in server.items
    delete = [(p, h) for verb, p, h in server.requests if verb == "DELETE"][-1]
    assert delete[1].get("If-Match") == '"etag-c1"'
    lines = []
    store._log = lines.append
    store.delete("/card/default/c1.vcf")
    assert lines == ["/card/default/c1.vcf was already gone from the server"]


def test_conflicts_and_server_errors_become_store_errors(server, store):
    store.list()
    server.items["/card/default/ada.vcf"] = Item('"etag-ada-changed"', ADA, VCARD_TYPE)
    with pytest.raises(StoreError) as info:
        store.update("/card/default/ada.vcf", Contact(first_name="Ada"))
    assert "changed on the server" in str(info.value)
    with pytest.raises(StoreError):
        store.delete("/card/default/ada.vcf")
    fresh = CardDavContactsStore(dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5), addressbook="Contacts")
    assert fresh.update("/card/default/ada.vcf", Contact(first_name="Fetched")) == server.items["/card/default/ada.vcf"].etag
    assert "FN:Fetched" in server.items["/card/default/ada.vcf"].data and "UID:ada-uid" in server.items["/card/default/ada.vcf"].data
    with pytest.raises(StoreError):
        fresh.update("/card/default/missing.vcf", Contact(first_name="x"))
    with pytest.raises(StoreError) as info:
        fresh.update("/card/default/group.vcf", Contact(first_name="x"))       # not a person's card
    assert "group" in str(info.value)
    with pytest.raises(StoreError):
        fresh.update("/card/default/broken.vcf", Contact(first_name="x"))      # unreadable on the server
    for bad in ("", "  ", "/card/a b.vcf"):
        with pytest.raises(StoreError):
            fresh.update(bad, Contact())
    wrong = CardDavContactsStore(dav.DavClient(server.base_url, USERNAME, "wrong", timeout=5))
    with pytest.raises(StoreError) as info:
        wrong.list()
    assert "wrong" not in str(info.value) and PASSWORD not in str(info.value)
    with pytest.raises(StoreError):
        CardDavContactsStore(dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5), addressbook="Nope").list()


def test_backend_build_reads_settings_and_secrets(server, tmp_path):
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    account = Account("c", "contacts", "carddav", (("url", server.base_url), ("username", USERNAME), ("addressbook", "Contacts")))
    store = carddav.build(account, {"password": PASSWORD}, context)
    assert isinstance(store, CardDavContactsStore) and [i.id for i in store.list() if not i.unreadable] == [
        "/card/default/ada.vcf", "/card/default/c1.vcf"]
    with pytest.raises(AccountError):
        carddav.build(account, {}, context)                                      # no password
    with pytest.raises(AccountError):
        carddav.build(Account("c", "contacts", "carddav", (("url", "not a url"), ("username", "u"))), {"password": "p"}, context)
    recorded = []

    def transport(request):
        recorded.append(request)
        return dav.Response(500, (), b"nope")

    hooked = carddav.build(account, {"password": PASSWORD}, BuildContext(log=lambda _l: None, sync_dir=tmp_path,
                                                                          save_secrets=lambda _c: None,
                                                                          extra={"dav_transport": transport}))
    with pytest.raises(StoreError):
        hooked.list()
    assert recorded and recorded[0].method == "PROPFIND"
    assert carddav.BACKEND.key == "carddav" and carddav.BACKEND.login is None
    assert [s.key for s in carddav.BACKEND.settings] == ["url", "username", "password", "addressbook"]
    assert carddav.BACKEND.missing_settings(Account("c", "contacts", "carddav"), {}) == ("url", "username", "password")
    assert carddav.BACKEND.missing_settings(account, {"password": "x"}) == ()
