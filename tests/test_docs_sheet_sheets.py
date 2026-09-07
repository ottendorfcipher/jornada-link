import json
from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Document
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.documents import sheets
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpClient, HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

FILES = "/drive/v3/files"
VALUES = "/v4/spreadsheets/{}/values/A1:ZZ10000"
VALUE_ROUTES = {
    ("GET", VALUES.format("s1")): json_response(200, {"values": [["Item", "Qty", "Ok"], ["Tea", 2, True], ["Milk", "1.5", "", ""], []]}),
    ("GET", VALUES.format("s2")): json_response(200, {"range": "Sheet1!A1:ZZ10000"}),
}


def query(request):
    return {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}


def drive_files(folder_found=True):
    def handler(request):
        q = query(request)["q"]
        if sheets.FOLDER_MIME in q:
            assert "name = 'Jor\\'nada'" in q and "'root' in parents" in q
            return json_response(200, {"files": [{"id": "fold1"}] if folder_found else []})
        assert q.startswith("'fold1' in parents") and sheets.SPREADSHEET_MIME in q
        if query(request).get("pageToken") == "p2":
            return json_response(200, {"files": [{"id": "s2", "name": "Empty", "version": 1}]})
        return json_response(200, {"files": [{"id": "s1", "name": "Budget", "modifiedTime": "2026-09-06T10:00:00Z",
                                              "version": "3"}], "nextPageToken": "p2"})
    return handler


def store(routes, folder_id=None, folder_found=True, log=None):
    routes = {("GET", FILES): drive_files(folder_found), **routes}
    transport, seen = fake_transport(routes, default=HttpResponse(403, (), b'{"error":"forbidden"}'))
    drive = HttpClient(sheets.DRIVE_URL, transport=transport, auth=lambda: "Bearer t")
    api = HttpClient(sheets.SHEETS_URL, transport=transport, auth=lambda: "Bearer t")
    return sheets.SheetsStore(drive, api, folder_id, "Jor'nada", log=(log if log is not None else []).append), seen


def test_list_finds_the_folder_pages_and_reads_first_sheet_values():
    sheets_store, seen = store(VALUE_ROUTES)
    items = sheets_store.list()
    assert [(i.id, i.record.name, i.record.text, i.version) for i in items] == [
        ("s1", "Budget", "Item,Qty,Ok\nTea,2,TRUE\nMilk,1.5\n", "3"), ("s2", "Empty", "", "1")]
    assert items[0].record.kind == "sheet" and items[0].record.modified is not None and items[1].record.modified is None
    values_request = next(r for r in seen if "/values/" in r.url)
    assert query(values_request) == {"valueRenderOption": "FORMATTED_VALUE"}
    assert query(seen[1])["fields"] == sheets.FILE_FIELDS and query(seen[1])["pageSize"] == "100"
    assert len([r for r in seen if urlsplit(r.url).path == FILES]) == 3  # folder lookup + two pages
    sheets_store.list()
    assert len([r for r in seen if sheets.FOLDER_MIME in query(r).get("q", "")]) == 1  # the folder id is cached


def test_folder_is_created_when_missing_and_folder_id_setting_skips_the_lookup():
    log = []
    routes = {("POST", FILES): json_response(200, {"id": "fold1"}), **VALUE_ROUTES}
    sheets_store, seen = store(routes, folder_found=False, log=log)
    assert len(sheets_store.list()) == 2
    created = next(r for r in seen if r.method == "POST")
    assert json.loads(created.body) == {"name": "Jor'nada", "mimeType": sheets.FOLDER_MIME, "parents": ["root"]}
    assert any("created Drive folder" in line for line in log)
    direct, seen = store(VALUE_ROUTES, folder_id="fold1")
    assert len(direct.list()) == 2
    assert all(sheets.FOLDER_MIME not in query(r).get("q", "") for r in seen)


def test_create_update_and_delete():
    routes = {
        ("POST", "/v4/spreadsheets"): json_response(200, {"spreadsheetId": "s9"}),
        ("PATCH", FILES + "/s9"): json_response(200, {"id": "s9", "parents": ["fold1"]}),
        ("PUT", VALUES.format("s9")): json_response(200, {"updatedCells": 4}),
        ("POST", VALUES.format("s1") + ":clear"): json_response(200, {}),
        ("PUT", VALUES.format("s1")): json_response(200, {"updatedCells": 2}),
        ("DELETE", FILES + "/s1"): HttpResponse(204),
    }
    sheets_store, seen = store(routes)
    assert sheets_store.create(Document("New", "a,b\n1,x\n", "sheet")) == "s9"
    created = next(r for r in seen if r.method == "POST" and r.url.endswith("/spreadsheets"))
    moved, written = next(r for r in seen if r.method == "PATCH"), seen[-1]
    assert json.loads(created.body) == {"properties": {"title": "New"}}
    assert moved.method == "PATCH" and query(moved) == {"addParents": "fold1", "removeParents": "root", "fields": "id,parents"}
    assert query(written) == {"valueInputOption": "USER_ENTERED"}
    assert json.loads(written.body) == {"range": sheets.RANGE, "majorDimension": "ROWS", "values": [["a", "b"], ["1", "x"]]}
    assert sheets_store.update("s1", Document("Budget", "Item\nTea\n", "sheet")) is None
    assert seen[-2].method == "POST" and seen[-2].url.endswith(":clear") and json.loads(seen[-1].body)["values"] == [["Item"], ["Tea"]]
    sheets_store.update("s1", Document("Budget", "", "sheet"))
    assert seen[-1].url.endswith(":clear")  # nothing to write after clearing
    sheets_store.delete("s1")
    assert seen[-1].method == "DELETE" and urlsplit(seen[-1].url).path == FILES + "/s1"


def test_failures_become_store_errors():
    sheets_store, _seen = store({})
    with pytest.raises(StoreError):
        sheets_store.list()
    with pytest.raises(StoreError):
        sheets_store.create(Document("x", "a\n", "sheet"))
    with pytest.raises(StoreError):
        sheets_store.delete("s1")
    no_id, _ = store({("POST", "/v4/spreadsheets"): json_response(200, {})})
    with pytest.raises(StoreError):
        no_id.create(Document("x", "a\n", "sheet"))
    no_folder, _ = store({("POST", FILES): json_response(200, {})}, folder_found=False)
    with pytest.raises(StoreError):
        no_folder.list()


def test_backend_spec_and_build(tmp_path):
    assert sheets.BACKEND.key == "sheets" and sheets.BACKEND.login is not None
    assert [s.key for s in sheets.BACKEND.settings] == ["client_id", "client_secret", TOKEN_KEY, "folder_id", "folder_name"]
    transport, seen = fake_transport({("GET", FILES): json_response(200, {"files": []})})
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None, http_transport=transport)
    secrets = {TOKEN_KEY: Token("tok", "rt", 9e9).to_dict()}
    built = sheets.build(Account("a", "documents", "sheets", (("client_id", "cid"), ("folder_id", "abc_-1"))), secrets, context)
    assert isinstance(built, sheets.SheetsStore) and built.list() == ()
    assert seen[0].header("Authorization") == "Bearer tok" and "'abc_-1' in parents" in query(seen[0])["q"]
    with pytest.raises(AccountError):
        sheets.build(Account("a", "documents", "sheets", (("client_id", "cid"), ("folder_id", "x' or 1"))), secrets, context)
