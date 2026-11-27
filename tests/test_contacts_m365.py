import json
import time
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Address, Contact
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.contacts.m365 import (BACKEND, SCOPES, SELECT_FIELDS, build, contact_of, payload_of, phone_slots,
                                        provider_for, tenant_of)
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

CONTACTS = "/v1.0/me/contacts"
FOLDERS = "/v1.0/me/contactFolders"
ENTRY = {
    "id": "c1", "changeKey": "ck1", "givenName": "Ada", "middleName": "King", "surname": "Lovelace", "title": "Countess",
    "generation": "PhD", "displayName": "Ada Lovelace", "companyName": "Analytical", "jobTitle": "Mathematician",
    "department": "R&D", "officeLocation": "Room 1",
    "emailAddresses": [{"name": "Ada", "address": "a@x.org"}, {"address": "b@x.org"}, {"address": "c@x.org"}, {"address": "d@x.org"}],
    "businessPhones": ["1", "2"], "homePhones": ["3", "4"], "mobilePhone": "5",
    "businessAddress": {"street": "HQ", "city": "City", "state": "", "postalCode": "", "countryOrRegion": "UK"},
    "homeAddress": {"street": "1 Rd", "city": "Town", "state": "ST", "postalCode": "123", "countryOrRegion": "UK"},
    "otherAddress": {},
    "birthday": "1815-12-10T00:00:00Z", "personalNotes": "note\r\nmore", "businessHomePage": "https://x.org",
    "spouseName": "William", "children": ["Byron", "Annabella"], "assistantName": "Charles", "categories": ["Friends", "Math"],
}
EXPECTED = Contact(
    first_name="Ada", last_name="Lovelace", middle_name="King", title="Countess", suffix="PhD", full_name="Ada Lovelace",
    company="Analytical", job_title="Mathematician", department="R&D", office="Room 1", emails=("a@x.org", "b@x.org", "c@x.org"),
    phones=(("work", "1"), ("work2", "2"), ("home", "3"), ("home2", "4"), ("mobile", "5")),
    addresses=(Address("work", "HQ", "City", "", "", "UK"), Address("home", "1 Rd", "Town", "ST", "123", "UK")),
    birthday=date(1815, 12, 10), spouse="William", children="Byron, Annabella", assistant="Charles",
    web_page="https://x.org", notes="note\nmore", categories=("Friends", "Math"), uid="c1",
)


def query(request):
    return {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}


def store(routes, tmp_path, log=None, **settings):
    transport, seen = fake_transport(routes)
    context = BuildContext(log=log or (lambda _l: None), sync_dir=tmp_path, save_secrets=lambda _c: None,
                           http_transport=transport)
    account = Account("m", "contacts", "m365", tuple(sorted({"client_id": "cid", **settings}.items())))
    return build(account, {TOKEN_KEY: Token("tok", "refresh", time.time() + 3600).to_dict()}, context), seen


# -- mapping ---------------------------------------------------------------------------------
def test_contact_of_maps_the_graph_resource():
    assert contact_of(ENTRY) == EXPECTED
    assert contact_of({"id": "c2"}) == Contact(uid="c2")
    assert contact_of({"id": "c3", "givenName": "Ada", "middleName": "K", "surname": "Lovelace"}).full_name == "Ada K Lovelace"
    assert contact_of({"id": "c4", "birthday": "garbage", "children": "not a list", "businessPhones": [1, "", " 7 "]}) == \
        Contact(phones=(("work", "7"),), uid="c4")


def test_payload_of_fills_graph_fields_and_uses_free_phone_slots():
    payload = payload_of(EXPECTED)
    assert payload["displayName"] == "Ada Lovelace" and payload["givenName"] == "Ada" and payload["generation"] == "PhD"
    assert payload["emailAddresses"] == [{"address": "a@x.org"}, {"address": "b@x.org"}, {"address": "c@x.org"}]
    assert payload["businessPhones"] == ["1", "2"] and payload["homePhones"] == ["3", "4"] and payload["mobilePhone"] == "5"
    assert payload["businessAddress"] == {"street": "HQ", "city": "City", "state": "", "postalCode": "", "countryOrRegion": "UK"}
    assert payload["homeAddress"]["postalCode"] == "123" and payload["otherAddress"] is None
    assert payload["birthday"] == "1815-12-10T00:00:00Z" and payload["children"] == ["Byron", "Annabella"]
    assert payload["personalNotes"] == "note\nmore" and payload["categories"] == ["Friends", "Math"]
    with_fax = Contact(first_name="F", phones=(("work", "1"), ("work_fax", "6"), ("home_fax", "7"), ("pager", "8"), ("car", "9")))
    fax_payload = payload_of(with_fax)
    assert fax_payload["businessPhones"] == ["1", "6"] and fax_payload["homePhones"] == ["7"]      # faxes take free slots
    assert fax_payload["mobilePhone"] is None and "8" not in json.dumps(fax_payload) and "9" not in json.dumps(fax_payload)
    assert phone_slots((("work", "1"), ("work2", "2"), ("work_fax", "6")), ("work", "work2"), "work_fax", 2) == ["1", "2"]
    empty = payload_of(Contact())
    assert "displayName" not in empty and empty["birthday"] is None and empty["homeAddress"] is None
    assert empty["emailAddresses"] == [] and empty["children"] == [] and empty["mobilePhone"] is None
    assert payload_of(Contact(children="Tom and Anna"))["children"] == ["Tom and Anna"]


# -- the store against a fake transport ------------------------------------------------------
def test_list_uses_the_default_folder_and_follows_next_links(tmp_path):
    def contacts(request):
        params = query(request)
        if "$skiptoken" in params:
            return json_response(200, {"value": [{"id": "c2", "changeKey": "ck2", "givenName": "Bob"}, {"givenName": "no id"}]})
        assert params == {"$top": "100", "$select": SELECT_FIELDS}
        return json_response(200, {"value": [ENTRY], "@odata.nextLink": f"https://graph.microsoft.com{CONTACTS}?$skiptoken=abc"})

    remote, seen = store({("GET", CONTACTS): contacts}, tmp_path)
    items = remote.list()
    assert [(i.id, i.version) for i in items] == [("c1", "ck1"), ("c2", "ck2")]
    assert items[0].record == EXPECTED and items[1].record == Contact(first_name="Bob", full_name="Bob", uid="c2")
    assert [r.method for r in seen] == ["GET", "GET"] and all(r.header("Authorization") == "Bearer tok" for r in seen)
    assert remote.name == "m365-contacts" and remote.contacts_path() == "/me/contacts"


def test_folder_is_resolved_by_display_name(tmp_path):
    def folders(request):
        params = query(request)
        if "$skiptoken" in params:
            return json_response(200, {"value": [{"id": "f2", "displayName": "Handheld"}]})
        assert params == {"$select": "id,displayName", "$top": "100"}
        return json_response(200, {"value": [{"id": "f1", "displayName": "Other"}],
                                   "@odata.nextLink": f"https://graph.microsoft.com{FOLDERS}?$skiptoken=x"})

    routes = {("GET", FOLDERS): folders, ("GET", f"{FOLDERS}/f2/contacts"): json_response(200, {"value": [ENTRY]}),
              ("POST", f"{FOLDERS}/f2/contacts"): json_response(201, {"id": "new", "changeKey": "ck"})}
    remote, seen = store(routes, tmp_path, folder="handheld")
    assert [i.id for i in remote.list()] == ["c1"] and remote.contacts_path() == "/me/contactFolders/f2/contacts"
    assert remote.create(Contact(first_name="Bob")) == "new"
    assert [urlsplit(r.url).path for r in seen] == [FOLDERS, FOLDERS, f"{FOLDERS}/f2/contacts", f"{FOLDERS}/f2/contacts"]
    missing, _ = store({("GET", FOLDERS): json_response(200, {"value": [{"id": "f1", "displayName": "Other"}]})}, tmp_path, folder="Nope")
    with pytest.raises(StoreError) as info:
        missing.list()
    assert "'Nope'" in str(info.value) and "Other" in str(info.value)


def test_create_update_delete(tmp_path):
    posts, patches = [], []

    def create(request):
        posts.append(json.loads(request.body))
        return json_response(201, {"id": "new1", "changeKey": "ck-new"})

    def patch(request):
        patches.append((urlsplit(request.url).path, json.loads(request.body)))
        return json_response(200, {"id": "c1", "changeKey": "ck2"})

    routes = {("POST", CONTACTS): create, ("PATCH", f"{CONTACTS}/c1"): patch,
              ("DELETE", f"{CONTACTS}/c1"): HttpResponse(204), ("DELETE", f"{CONTACTS}/gone"): json_response(404, {}),
              ("DELETE", f"{CONTACTS}/locked"): json_response(423, {"error": {"code": "locked"}})}
    logs = []
    remote, seen = store(routes, tmp_path, logs.append)
    assert remote.create(EXPECTED) == "new1" and posts == [payload_of(EXPECTED)]
    assert remote.update("c1", Contact(first_name="Ada", last_name="Byron")) == "ck2"
    assert patches == [(f"{CONTACTS}/c1", payload_of(Contact(first_name="Ada", last_name="Byron")))]
    assert patches[0][1]["surname"] == "Byron" and patches[0][1]["displayName"] == "Ada Byron"
    remote.delete("c1")
    remote.delete("gone")
    assert logs[-1] == "Microsoft 365 contact gone was already gone"
    with pytest.raises(StoreError):
        remote.delete("locked")
    assert [r.method for r in seen[-3:]] == ["DELETE"] * 3
    for bad in ("", "  ", "a b"):
        with pytest.raises(StoreError):
            remote.update(bad, Contact())
        with pytest.raises(StoreError):
            remote.delete(bad)


def test_failures_surface_as_store_errors(tmp_path):
    remote, _ = store({("GET", CONTACTS): json_response(403, {"error": {"code": "accessDenied"}})}, tmp_path)
    with pytest.raises(StoreError) as info:
        remote.list()
    assert "403" in str(info.value) and "Bearer" not in str(info.value)
    remote, _ = store({("GET", CONTACTS): HttpResponse(200, (), b"<html>")}, tmp_path)
    with pytest.raises(StoreError):
        remote.list()
    remote, _ = store({("POST", CONTACTS): json_response(201, {"changeKey": "no id"})}, tmp_path)
    with pytest.raises(StoreError):
        remote.create(Contact(first_name="x"))
    with pytest.raises(StoreError):
        remote.update("nowhere", Contact(first_name="x"))
    remote, _ = store({("GET", CONTACTS): lambda r: json_response(200, {"value": [], "@odata.nextLink": r.url})}, tmp_path)
    with pytest.raises(StoreError) as info:
        remote.list()
    assert "did not end" in str(info.value)


def test_backend_spec_and_provider(tmp_path):
    assert BACKEND.key == "m365" and BACKEND.title == "Microsoft 365 contacts" and BACKEND.login is not None
    assert [s.key for s in BACKEND.settings] == ["client_id", "client_secret", "token", "tenant", "folder"]
    assert SCOPES == ("Contacts.ReadWrite", "offline_access") and "fax" in BACKEND.notes
    assert provider_for(Account("a", "contacts", "m365")).token_url.startswith("https://login.microsoftonline.com/common/")
    assert "/contoso.example/" in provider_for(Account("a", "contacts", "m365", (("tenant", "contoso.example"),))).auth_url
    assert tenant_of(Account("a", "contacts", "m365", (("tenant", "  "),))) == "common"
    with pytest.raises(AccountError):
        tenant_of(Account("a", "contacts", "m365", (("tenant", "bad tenant/"),)))
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    with pytest.raises(AccountError):
        BACKEND.login(Account("a", "contacts", "m365"), {}, context)
    assert BACKEND.missing_settings(Account("a", "contacts", "m365"), {}) == ("client_id", "token")
