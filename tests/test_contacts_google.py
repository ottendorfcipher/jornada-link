import json
import time
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Address, Contact
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.contacts.google import (BACKEND, PERSON_FIELDS, SCOPES, UPDATE_FIELDS, build, date_of, is_member,
                                          payload_of, person_of)
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

CONNECTIONS = "/v1/people/me/connections"
GROUPS = "/v1/contactGroups"
CREATE = "/v1/people:createContact"
GROUP_RN = "contactGroups/abc123"

PERSON = {
    "resourceName": "people/c1", "etag": "e1",
    "names": [{"givenName": "Ada", "familyName": "Lovelace", "middleName": "King", "honorificPrefix": "Countess",
               "honorificSuffix": "PhD", "displayName": "Ada Lovelace"}],
    "organizations": [{"name": "Analytical", "title": "Mathematician", "department": "R&D", "location": "Room 1"}],
    "emailAddresses": [{"value": "b@x.org"}, {"value": "a@x.org", "metadata": {"primary": True}}, {"value": "c@x.org"},
                       {"value": "d@x.org"}],
    "phoneNumbers": [{"value": "5", "type": "mobile"}, {"value": "1", "type": "work"}, {"value": "2", "type": "main"},
                     {"value": "3", "type": "home"}, {"value": "6", "type": "workFax"}, {"value": "7", "type": "homeFax"},
                     {"value": "8", "type": "pager"}, {"value": "9", "type": "car"}, {"value": "10", "type": "radio"},
                     {"value": "11", "type": "assistant"}],
    "addresses": [{"type": "home", "streetAddress": "1 Rd", "city": "Town", "region": "ST", "postalCode": "123", "country": "UK"},
                  {"type": "work", "poBox": "PO 9", "streetAddress": "HQ", "city": "City", "countryCode": "GB"}],
    "birthdays": [{"date": {"year": 1815, "month": 12, "day": 10}}],
    "biographies": [{"value": "note\r\nmore", "contentType": "TEXT_PLAIN"}],
    "urls": [{"value": "https://x.org"}],
    "events": [{"type": "anniversary", "date": {"month": 7, "day": 8}}, {"type": "anniversary", "date": {"year": 1835, "month": 7, "day": 8}}],
    "relations": [{"person": "William", "type": "spouse"}, {"person": "Byron", "type": "child"},
                  {"person": "Annabella", "type": "child"}, {"person": "Charles", "type": "assistant"}],
    "memberships": [{"contactGroupMembership": {"contactGroupResourceName": GROUP_RN}}],
}
EXPECTED = Contact(
    first_name="Ada", last_name="Lovelace", middle_name="King", title="Countess", suffix="PhD",
    full_name="Ada King Lovelace", company="Analytical",
    job_title="Mathematician", department="R&D", office="Room 1", emails=("a@x.org", "b@x.org", "c@x.org"),
    phones=(("work", "1"), ("work2", "2"), ("home", "3"), ("mobile", "5"), ("work_fax", "6"), ("home_fax", "7"),
            ("pager", "8"), ("car", "9"), ("radio", "10"), ("assistant", "11")),
    addresses=(Address("home", "1 Rd", "Town", "ST", "123", "UK"), Address("work", "PO 9\nHQ", "City", "", "", "GB")),
    birthday=date(1815, 12, 10), anniversary=date(1835, 7, 8), spouse="William", children="Byron, Annabella",
    assistant="Charles", web_page="https://x.org", notes="note\nmore", uid="people/c1",
)


def query(request):
    return {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}


def store(routes, tmp_path, log=None, **settings):
    transport, seen = fake_transport(routes)
    context = BuildContext(log=log or (lambda _l: None), sync_dir=tmp_path, save_secrets=lambda _c: None,
                           http_transport=transport)
    account = Account("g", "contacts", "google", tuple(sorted({"client_id": "cid", **settings}.items())))
    return build(account, {TOKEN_KEY: Token("tok", "r", time.time() + 3600).to_dict()}, context), seen


# -- mapping -------------------------------------------------------------------------------
def test_person_of_maps_the_people_api_resource():
    assert person_of(PERSON) == EXPECTED
    assert person_of({"resourceName": "people/c2"}) == Contact(uid="people/c2")
    yearless = {**PERSON, "birthdays": [{"date": {"month": 1, "day": 31}}], "events": [], "relations": []}
    assert person_of(yearless).birthday is None and person_of(yearless).anniversary is None
    assert person_of(yearless).spouse == "" and person_of(yearless).children == ""
    other = person_of({"resourceName": "people/c3", "phoneNumbers": [{"value": "1", "type": "other"}, {"value": "2", "type": "work"}]})
    assert other.phones == (("work", "1"), ("work2", "2"))
    assert date_of({"year": "1999", "month": 2, "day": 30}) is None and date_of("1999-01-01") is None
    assert is_member(PERSON, GROUP_RN) and not is_member(PERSON, "contactGroups/other")


def test_payload_of_writes_every_field_and_clears_missing_ones():
    payload = payload_of(EXPECTED)
    assert payload["names"] == [{"givenName": "Ada", "familyName": "Lovelace", "middleName": "King",
                                 "honorificPrefix": "Countess", "honorificSuffix": "PhD"}]
    assert payload["organizations"] == [{"name": "Analytical", "title": "Mathematician", "department": "R&D", "location": "Room 1"}]
    assert payload["emailAddresses"] == [{"value": "a@x.org"}, {"value": "b@x.org"}, {"value": "c@x.org"}]
    assert [(p["type"], p["value"]) for p in payload["phoneNumbers"]] == [
        ("work", "1"), ("work", "2"), ("home", "3"), ("mobile", "5"), ("workFax", "6"), ("homeFax", "7"),
        ("pager", "8"), ("car", "9"), ("radio", "10"), ("assistant", "11")]
    assert payload["addresses"] == [
        {"type": "home", "streetAddress": "1 Rd", "city": "Town", "region": "ST", "postalCode": "123", "country": "UK"},
        {"type": "work", "streetAddress": "PO 9\nHQ", "city": "City", "region": "", "postalCode": "", "country": "GB"}]
    assert payload["birthdays"] == [{"date": {"year": 1815, "month": 12, "day": 10}}]
    assert payload["biographies"] == [{"value": "note\nmore", "contentType": "TEXT_PLAIN"}]
    assert payload["urls"] == [{"value": "https://x.org"}]
    assert payload["events"] == [{"type": "anniversary", "date": {"year": 1835, "month": 7, "day": 8}}]
    assert payload["relations"] == [{"person": "William", "type": "spouse"}, {"person": "Byron", "type": "child"},
                                    {"person": "Annabella", "type": "child"}, {"person": "Charles", "type": "assistant"}]
    empty = payload_of(Contact(company="ACME"))
    assert empty["names"] == [] and empty["organizations"] == [{"name": "ACME", "title": "", "department": "", "location": ""}]
    assert all(empty[key] == [] for key in ("emailAddresses", "phoneNumbers", "addresses", "birthdays", "biographies",
                                            "urls", "events", "relations"))


# -- the store against a fake transport ----------------------------------------------------
def test_list_pages_and_sends_person_fields(tmp_path):
    def connections(request):
        params = query(request)
        assert params["personFields"] == PERSON_FIELDS and params["pageSize"] == "200"
        if params.get("pageToken") == "p2":
            return json_response(200, {"connections": [{"resourceName": "people/c2", "etag": "e2", "names": [{"givenName": "Bob"}]}]})
        return json_response(200, {"connections": [PERSON, {"names": [{"givenName": "no resource name"}]}], "nextPageToken": "p2"})

    remote, seen = store({("GET", CONNECTIONS): connections}, tmp_path)
    items = remote.list()
    assert [(i.id, i.version) for i in items] == [("people/c1", "e1"), ("people/c2", "e2")]
    assert items[0].record == EXPECTED and items[1].record == Contact(first_name="Bob", full_name="Bob", uid="people/c2")
    assert len(seen) == 2 and all(r.header("Authorization") == "Bearer tok" for r in seen)
    assert remote.name == "google-contacts" and remote.group_resource() is None


def test_group_filters_the_listing_and_is_created_when_missing(tmp_path):
    created = []

    def groups(request):
        return json_response(200, {"contactGroups": [{"resourceName": "contactGroups/x", "name": "Other"},
                                                     {"resourceName": GROUP_RN, "name": "Jornada", "formattedName": "Jornada"}]})

    def create_group(request):
        created.append(json.loads(request.body))
        return json_response(200, {"resourceName": "contactGroups/new", "name": "Handheld"})

    outsider = {"resourceName": "people/c9", "etag": "e9", "names": [{"givenName": "Out"}]}
    routes = {("GET", GROUPS): groups, ("GET", CONNECTIONS): json_response(200, {"connections": [PERSON, outsider]})}
    remote, seen = store(routes, tmp_path, group="jornada")
    assert [i.id for i in remote.list()] == ["people/c1"] and remote.group_resource() == GROUP_RN
    assert [urlsplit(r.url).path for r in seen] == [GROUPS, CONNECTIONS]
    assert [i.id for i in remote.list()] == ["people/c1"] and len(seen) == 3        # the group id is cached

    logs = []
    routes = {("GET", GROUPS): json_response(200, {"contactGroups": []}), ("POST", GROUPS): create_group,
              ("GET", CONNECTIONS): json_response(200, {"connections": [PERSON]})}
    remote, _ = store(routes, tmp_path, logs.append, group="Handheld")
    assert remote.list() == () and created == [{"contactGroup": {"name": "Handheld"}}]
    assert remote.group_resource() == "contactGroups/new" and any("created Google Contacts group Handheld" in l for l in logs)


def test_create_update_delete(tmp_path):
    posts, patches = [], []

    def create(request):
        posts.append((query(request), json.loads(request.body)))
        return json_response(200, {"resourceName": "people/new", "etag": "e-new"})

    def update(request):
        patches.append((urlsplit(request.url).path, query(request), json.loads(request.body)))
        return json_response(200, {"resourceName": "people/c1", "etag": "e1b"})

    routes = {("GET", GROUPS): json_response(200, {"contactGroups": [{"resourceName": GROUP_RN, "name": "Jornada"}]}),
              ("GET", CONNECTIONS): json_response(200, {"connections": [PERSON]}),
              ("POST", CREATE): create, ("PATCH", "/v1/people/c1:updateContact"): update,
              ("PATCH", "/v1/people/c7:updateContact"): update,
              ("GET", "/v1/people/c7"): json_response(200, {"resourceName": "people/c7", "etag": "e7"}),
              ("DELETE", "/v1/people/c1:deleteContact"): HttpResponse(204),
              ("DELETE", "/v1/people/gone:deleteContact"): json_response(404, {"error": {"code": 404}}),
              ("DELETE", "/v1/people/locked:deleteContact"): json_response(403, {"error": {"code": 403}})}
    logs = []
    remote, seen = store(routes, tmp_path, logs.append, group="Jornada")
    assert remote.create(Contact(first_name="Bob", emails=("bob@x",))) == "people/new"
    assert posts[0][0] == {"personFields": PERSON_FIELDS}
    assert posts[0][1]["memberships"] == [{"contactGroupMembership": {"contactGroupResourceName": GROUP_RN}}]
    assert posts[0][1]["names"] == [{"givenName": "Bob", "familyName": "", "middleName": "", "honorificPrefix": "", "honorificSuffix": ""}]
    remote.list()
    assert remote.update("people/c1", Contact(first_name="Ada", last_name="Byron")) == "e1b"
    path, params, body = patches[-1]
    assert path == "/v1/people/c1:updateContact" and params == {"updatePersonFields": UPDATE_FIELDS}
    assert body["etag"] == "e1" and body["names"][0]["familyName"] == "Byron" and body["emailAddresses"] == []
    assert remote.update("people/c7", Contact(first_name="Fetched")) == "e1b"      # never listed: the etag is fetched first
    assert query([r for r in seen if urlsplit(r.url).path == "/v1/people/c7"][0]) == {"personFields": "metadata"}
    assert patches[-1][2]["etag"] == "e7"
    remote.delete("people/c1")
    remote.delete("people/gone")
    assert logs[-1] == "Google Contacts contact people/gone was already gone"
    with pytest.raises(StoreError):
        remote.delete("people/locked")
    for bad in ("", "c1", "people/c 1", "contactGroups/x"):
        with pytest.raises(StoreError):
            remote.update(bad, Contact())
        with pytest.raises(StoreError):
            remote.delete(bad)


def test_failures_and_spec(tmp_path):
    remote, _ = store({("GET", CONNECTIONS): json_response(403, {"error": {"message": "denied"}})}, tmp_path)
    with pytest.raises(StoreError) as info:
        remote.list()
    assert "403" in str(info.value) and "Bearer" not in str(info.value) and "Google Contacts" in str(info.value)
    remote, _ = store({("GET", CONNECTIONS): HttpResponse(200, (), b"<html>")}, tmp_path)
    with pytest.raises(StoreError):
        remote.list()
    remote, _ = store({("POST", CREATE): json_response(200, {"etag": "no resource name"})}, tmp_path)
    with pytest.raises(StoreError):
        remote.create(Contact(first_name="x"))
    remote, _ = store({("GET", "/v1/people/c1"): json_response(200, {"resourceName": "people/c1"})}, tmp_path)
    with pytest.raises(StoreError):
        remote.update("people/c1", Contact(first_name="x"))       # no etag to send
    remote, _ = store({("GET", GROUPS): json_response(200, {"contactGroups": []}),
                       ("POST", GROUPS): json_response(500, {"error": {}})}, tmp_path, group="G")
    with pytest.raises(StoreError):
        remote.list()
    assert BACKEND.key == "google" and BACKEND.title == "Google Contacts" and BACKEND.login is not None
    assert SCOPES == ("https://www.googleapis.com/auth/contacts",)
    assert [s.key for s in BACKEND.settings] == ["client_id", "client_secret", "token", "group"]
    assert BACKEND.missing_settings(Account("g", "contacts", "google"), {}) == ("client_id", "token")
