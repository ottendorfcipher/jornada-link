from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Document
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.documents import excel
from jornada.sync.documents.xlsx import from_rows, to_rows
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpClient, HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

FOLDER = "/v1.0/me/drive/root:/Documents/Jornada:"
ITEMS = "/v1.0/me/drive/items"


def store(routes, path="Documents/Jornada", log=None):
    transport, seen = fake_transport(routes, default=HttpResponse(403, (), b'{"error":"forbidden"}'))
    http = HttpClient(excel.GRAPH_URL, transport=transport, auth=lambda: "Bearer t")
    return excel.ExcelStore(http, path, log=(log if log is not None else []).append, downloads=HttpClient(transport=transport)), seen


def listing_routes():
    page_one = {"value": [
        {"id": "i1", "name": "Budget.xlsx", "eTag": "e1", "file": {}, "lastModifiedDateTime": "2026-09-06T10:00:00Z",
         "@microsoft.graph.downloadUrl": "https://dl.example/budget"},
        {"id": "i3", "name": "Photo.png", "file": {}},
        {"id": "i4", "name": "Sub.xlsx", "folder": {}},
    ], "@odata.nextLink": excel.GRAPH_URL + "/next"}
    page_two = {"value": [{"id": "i2", "name": "notes.csv", "eTag": "e2", "file": {}},
                          {"id": "i5", "name": "broken.xlsx", "eTag": "e5", "file": {}}]}
    return {
        ("GET", FOLDER + "/children"): json_response(200, page_one),
        ("GET", "/v1.0/next"): json_response(200, page_two),
        ("GET", "/budget"): HttpResponse(200, (), from_rows((("Item", "Cost"), ("Tea", "3")))),
        ("GET", ITEMS + "/i2/content"): HttpResponse(200, (), "\ufeffItem,Cost\r\nMilk,1.5\r\n".encode("utf-8")),
        ("GET", ITEMS + "/i5/content"): HttpResponse(200, (), b"not a workbook"),
    }


def test_list_pages_downloads_and_skips_non_sheets():
    log = []
    excel_store, seen = store(listing_routes(), log=log)
    items = excel_store.list()
    assert [(i.id, i.record.name, i.record.text, i.version) for i in items] == [
        ("i1", "Budget", "Item,Cost\nTea,3\n", "e1"), ("i2", "notes", "Item,Cost\nMilk,1.5\n", "e2")]
    assert items[0].record.kind == "sheet" and items[0].record.modified is not None
    download = next(r for r in seen if "dl.example" in r.url)
    assert download.header("Authorization") is None  # pre-authenticated URL: no bearer token
    assert next(r for r in seen if r.url.endswith("/i2/content")).header("Authorization") == "Bearer t"
    assert parse_qs(urlsplit(seen[0].url).query) == {"$top": ["200"]}
    assert any("broken.xlsx" in line for line in log)


def test_create_update_and_delete_send_the_right_requests():
    routes = listing_routes()
    routes[("PUT", "/v1.0/me/drive/root:/Documents/Jornada/New%20Sheet.xlsx:/content")] = json_response(201, {"id": "i9", "eTag": "e9"})
    routes[("PUT", ITEMS + "/i2/content")] = json_response(200, {"id": "i2", "eTag": "e2b"})
    routes[("PUT", ITEMS + "/i9/content")] = json_response(200, {"id": "i9", "eTag": "e9b"})
    routes[("DELETE", ITEMS + "/i1")] = HttpResponse(204)
    excel_store, seen = store(routes)
    excel_store.list()
    assert excel_store.create(Document("New Sheet", "a,b\n1,x\n", "sheet")) == "i9"
    upload = seen[-1]
    assert upload.method == "PUT" and parse_qs(urlsplit(upload.url).query) == {"@microsoft.graph.conflictBehavior": ["rename"]}
    assert upload.header("Content-Type") == excel.XLSX_MIME and to_rows(upload.body) == (("a", "b"), ("1", "x"))
    assert excel_store.update("i2", Document("notes", "Item,Cost\nMilk,2\n", "sheet")) == "e2b"
    assert seen[-1].body == "\ufeffItem,Cost\r\nMilk,2\r\n".encode("utf-8") and seen[-1].header("Content-Type") == "text/csv"
    assert excel_store.update("i9", Document("New Sheet", "a\n", "sheet")) == "e9b"
    assert to_rows(seen[-1].body) == (("a",),)
    excel_store.delete("i1")
    assert seen[-1].method == "DELETE" and seen[-1].url.endswith("/items/i1")


def test_missing_folder_is_created_segment_by_segment():
    log = []
    routes = {
        ("GET", "/v1.0/me/drive/root:/Docs/Sub/Jornada:/children"): json_response(404, {"error": {"code": "itemNotFound"}}),
        ("POST", "/v1.0/me/drive/root/children"): json_response(409, {"error": {"code": "nameAlreadyExists"}}),
        ("POST", "/v1.0/me/drive/root:/Docs:/children"): json_response(201, {"id": "d1"}),
        ("POST", "/v1.0/me/drive/root:/Docs/Sub:/children"): json_response(201, {"id": "d2"}),
    }
    excel_store, seen = store(routes, path="/Docs/Sub/Jornada/", log=log)
    assert excel_store.list() == ()
    posts = [r for r in seen if r.method == "POST"]
    assert [urlsplit(r.url).path for r in posts] == ["/v1.0/me/drive/root/children", "/v1.0/me/drive/root:/Docs:/children",
                                                     "/v1.0/me/drive/root:/Docs/Sub:/children"]
    assert b'"name": "Jornada"' in posts[-1].body and b'"folder": {}' in posts[-1].body
    assert any("created OneDrive folder Docs/Sub/Jornada" in line for line in log)


def test_http_failures_become_store_errors():
    excel_store, _seen = store({})
    with pytest.raises(StoreError):
        excel_store.list()
    with pytest.raises(StoreError):
        excel_store.create(Document("x", "a\n", "sheet"))
    with pytest.raises(StoreError):
        excel_store.delete("i1")
    failing, _ = store({("GET", FOLDER + "/children"): json_response(200, {"value": [
        {"id": "i1", "name": "a.xlsx", "file": {}, "@microsoft.graph.downloadUrl": "https://dl.example/a"}]})})
    with pytest.raises(StoreError):
        failing.list()
    no_id, _ = store({("PUT", "/v1.0/me/drive/root:/Documents/Jornada/x.xlsx:/content"): json_response(201, {})})
    with pytest.raises(StoreError):
        no_id.create(Document("x", "a\n", "sheet"))


def test_backend_spec_and_build(tmp_path):
    assert excel.BACKEND.key == "excel" and excel.BACKEND.login is not None
    keys = [s.key for s in excel.BACKEND.settings]
    assert keys == ["client_id", "client_secret", TOKEN_KEY, "tenant", "path", "device_format"]
    assert excel.provider_for(Account("a", "documents", "excel")).auth_url.startswith("https://login.microsoftonline.com/common/")
    assert "contoso" in excel.provider_for(Account("a", "documents", "excel", (("tenant", "contoso"),))).token_url
    transport, seen = fake_transport({("GET", FOLDER + "/children"): json_response(200, {"value": []})})
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None, http_transport=transport)
    account = Account("a", "documents", "excel", (("client_id", "cid"),))
    built = excel.build(account, {TOKEN_KEY: Token("tok", "rt", 9e9).to_dict()}, context)
    assert isinstance(built, excel.ExcelStore) and built.list() == ()
    assert seen[0].header("Authorization") == "Bearer tok"
    assert excel.BACKEND.missing_settings(account, {}) == (TOKEN_KEY,)
