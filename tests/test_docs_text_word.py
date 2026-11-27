import json
import time
from urllib.parse import urlsplit

import pytest

from jornada.pim.models import Document
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.documents import docx
from jornada.sync.documents.word import BACKEND, DEFAULT_PATH, SCOPES, build, provider_for
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

BASE = "/v1.0/me/drive"
FOLDER = f"{BASE}/root:/Documents/Jornada:"
UPLOAD = f"{BASE}/root:/Documents/Jornada/Notes.docx:/content"


def store(routes, tmp_path, log=None, **settings):
    transport, seen = fake_transport(routes)
    context = BuildContext(log=log or (lambda _l: None), sync_dir=tmp_path, save_secrets=lambda _c: None,
                           http_transport=transport)
    account = Account("w", "documents", "word", tuple(sorted({"client_id": "cid", **settings}.items())))
    return build(account, {TOKEN_KEY: Token("tok", "refresh", time.time() + 3600).to_dict()}, context), seen


def entry(item_id, name, etag="e1"):
    return {"id": item_id, "name": name, "file": {"mimeType": "x"}, "eTag": etag, "size": 1,
            "lastModifiedDateTime": "2026-09-06T10:00:00.1234567Z"}


def test_backend_spec_and_provider(tmp_path):
    assert BACKEND.key == "word" and BACKEND.title == "Microsoft Word documents on OneDrive"
    assert [s.key for s in BACKEND.settings] == ["client_id", "client_secret", "token", "tenant", "path", "device_format"]
    assert SCOPES == ("Files.ReadWrite", "offline_access") and BACKEND.login is not None
    assert provider_for(Account("a", "documents", "word")).token_url.startswith("https://login.microsoftonline.com/common/")
    assert "/contoso/" in provider_for(Account("a", "documents", "word", (("tenant", "contoso"),))).auth_url
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    with pytest.raises(AccountError):
        BACKEND.login(Account("a", "documents", "word"), {}, context)


def test_list_pages_filters_and_reads_docx_and_txt(tmp_path):
    def children(request):
        query = urlsplit(request.url).query
        if "skiptoken" in query:
            return json_response(200, {"value": [entry("t1", "Plain.txt", "e2")]})
        assert "$select=id,name,file,lastModifiedDateTime,eTag,size" in query
        return json_response(200, {
            "value": [entry("d1", "Report.docx"), entry("p1", "skip.pdf"), {"id": "f1", "name": "Sub", "folder": {}}],
            "@odata.nextLink": f"https://graph.microsoft.com{FOLDER}/children?$skiptoken=abc"})
    routes = {("GET", f"{FOLDER}/children"): children,
              ("GET", f"{BASE}/items/d1/content"): HttpResponse(200, (), docx.from_text("Quarterly\n\nAll good.")),
              ("GET", f"{BASE}/items/t1/content"): HttpResponse(200, (), "plain é\r\ntext".encode("utf-8"))}
    remote, seen = store(routes, tmp_path)
    items = remote.list()
    assert [(i.id, i.version) for i in items] == [("d1", "e1"), ("t1", "e2")]
    assert items[0].record == Document("Report", "Quarterly\n\nAll good.", "text", items[0].record.modified)
    assert items[0].record.modified is not None
    assert items[1].record.name == "Plain" and items[1].record.text == "plain é\ntext"
    assert [r.method for r in seen] == ["GET"] * 4 and all(r.header("Authorization") == "Bearer tok" for r in seen)
    assert remote.path == DEFAULT_PATH


def test_custom_path_is_url_quoted(tmp_path):
    routes = {("GET", f"{BASE}/root:/Docs/HP%20Jornada:/children"): json_response(200, {"value": []})}
    remote, seen = store(routes, tmp_path, path="/Docs/HP Jornada/")
    assert remote.list() == () and remote.path == "Docs/HP Jornada"
    assert urlsplit(seen[0].url).path == f"{BASE}/root:/Docs/HP%20Jornada:/children"


def test_create_makes_the_folder_when_the_listing_found_none(tmp_path):
    made, uploads = [], []

    def mkdir(request):
        made.append((urlsplit(request.url).path, json.loads(request.body)))
        return json_response(409 if len(made) == 1 else 201, {"id": "fld"})

    def upload(request):
        uploads.append((request.header("Content-Type"), docx.to_text(request.body), urlsplit(request.url).query))
        return json_response(201, {"id": "new1", "name": "Notes.docx", "eTag": "e9"})

    routes = {("GET", f"{FOLDER}/children"): json_response(404, {"error": {"code": "itemNotFound"}}),
              ("POST", f"{BASE}/root/children"): mkdir, ("POST", f"{BASE}/root:/Documents:/children"): mkdir,
              ("PUT", UPLOAD): upload}
    logs = []
    remote, _seen = store(routes, tmp_path, logs.append)
    assert remote.list() == () and any("does not exist" in line for line in logs)
    assert remote.create(Document("Notes", "one\ntwo")) == "new1"
    folder = {"folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
    assert made == [(f"{BASE}/root/children", {"name": "Documents", **folder}),
                    (f"{BASE}/root:/Documents:/children", {"name": "Jornada", **folder})]
    assert uploads == [("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "one\ntwo",
                        "@microsoft.graph.conflictBehavior=rename")]
    assert remote.create(Document("Notes", "again")) == "new1" and len(made) == 2     # folder made once
    assert any("created OneDrive folder Documents/Jornada" in line for line in logs)


def test_update_and_delete(tmp_path):
    puts = []

    def put(request):
        puts.append((urlsplit(request.url).path, request.header("Content-Type"), request.body))
        return json_response(200, {"id": "x", "eTag": "e2"})

    routes = {("GET", f"{FOLDER}/children"): json_response(200, {"value": [entry("d1", "Report.docx"), entry("t1", "Plain.txt")]}),
              ("GET", f"{BASE}/items/d1/content"): HttpResponse(200, (), docx.from_text("a")),
              ("GET", f"{BASE}/items/t1/content"): HttpResponse(200, (), b"b"),
              ("PUT", f"{BASE}/items/d1/content"): put, ("PUT", f"{BASE}/items/t1/content"): put,
              ("DELETE", f"{BASE}/items/d1"): HttpResponse(204), ("DELETE", f"{BASE}/items/gone"): json_response(404, {}),
              ("DELETE", f"{BASE}/items/locked"): json_response(423, {"error": {"code": "locked"}})}
    remote, seen = store(routes, tmp_path)
    remote.list()
    assert remote.update("d1", Document("Report", "new\ntext")) == "e2"
    assert remote.update("t1", Document("Plain", "café")) == "e2"
    assert puts[0][0] == f"{BASE}/items/d1/content" and puts[0][1].startswith("application/vnd.openxml")
    assert docx.to_text(puts[0][2]) == "new\ntext"
    assert puts[1][1].startswith("text/plain") and puts[1][2] == "café".encode("utf-8")
    remote.delete("d1")
    remote.delete("gone")                       # already gone is not an error
    with pytest.raises(StoreError):
        remote.delete("locked")
    assert [r.method for r in seen[-3:]] == ["DELETE"] * 3


def test_failures_surface_as_store_errors(tmp_path):
    remote, _ = store({("GET", f"{FOLDER}/children"): json_response(403, {"error": {"code": "accessDenied"}})}, tmp_path)
    with pytest.raises(StoreError) as info:
        remote.list()
    assert "403" in str(info.value) and "Bearer" not in str(info.value)
    remote, _ = store({("GET", f"{FOLDER}/children"): HttpResponse(200, (), b"<html>")}, tmp_path)
    with pytest.raises(StoreError):
        remote.list()
    remote, _ = store({("PUT", UPLOAD): json_response(201, {"name": "Notes.docx"})}, tmp_path)
    with pytest.raises(StoreError):
        remote.create(Document("Notes", "x"))
    with pytest.raises(StoreError):
        remote.update("nowhere", Document("x", "y"))
    logs = []
    remote, _ = store({("GET", f"{FOLDER}/children"): json_response(200, {"value": [entry("d1", "Bad.docx")]}),
                       ("GET", f"{BASE}/items/d1/content"): HttpResponse(200, (), b"not a docx")}, tmp_path, logs.append)
    (bad,) = remote.list()
    assert bad.id == "d1" and bad.unreadable and "not a .docx" in bad.problem
    assert any("Bad.docx cannot be read" in line for line in logs)
