import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Document
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.documents.gdocs import BACKEND, DOC_MIME, FOLDER_MIME, SCOPES, build, multipart_related, query_literal
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY

FILES = "/drive/v3/files"
UPLOAD = "/upload/drive/v3/files"


def query(request):
    return {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}


def store(routes, tmp_path, log=None, **settings):
    transport, seen = fake_transport(routes)
    context = BuildContext(log=log or (lambda _l: None), sync_dir=tmp_path, save_secrets=lambda _c: None,
                           http_transport=transport)
    account = Account("g", "documents", "gdocs", tuple(sorted({"client_id": "cid", **settings}.items())))
    return build(account, {TOKEN_KEY: Token("tok", "r", time.time() + 3600).to_dict()}, context), seen


def test_folder_is_found_or_created(tmp_path):
    lookups, created = [], []

    def files(request):
        lookups.append(query(request)["q"])
        return json_response(200, {"files": []})

    def create_folder(request):
        created.append(json.loads(request.body))
        return json_response(200, {"id": "fld1"})

    logs = []
    remote, _ = store({("GET", FILES): files, ("POST", FILES): create_folder}, tmp_path, logs.append)
    assert remote.list() == ()
    assert created == [{"name": "Jornada", "mimeType": FOLDER_MIME}]
    assert lookups == [f"name='Jornada' and mimeType='{FOLDER_MIME}' and 'root' in parents and trashed=false",
                       f"'fld1' in parents and mimeType='{DOC_MIME}' and trashed=false"]
    assert any("created Drive folder Jornada" in line for line in logs)
    assert remote.list() == () and len(created) == 1                       # the id is cached

    def found(request):
        hit = "name='HP\\'s'" in query(request)["q"]
        return json_response(200, {"files": [{"id": "old", "name": "HP's"}] if hit else []})

    remote, seen = store({("GET", FILES): found}, tmp_path, folder_name="HP's")
    remote.list()
    assert "'old' in parents" in query(seen[1])["q"]
    remote, seen = store({("GET", FILES): json_response(200, {"files": []})}, tmp_path, folder_id="given")
    remote.list()
    assert len(seen) == 1 and "'given' in parents" in query(seen[0])["q"]


def test_list_pages_and_exports_plain_text(tmp_path):
    def files(request):
        params = query(request)
        assert params["fields"] == "nextPageToken,files(id,name,modifiedTime,version)" and params["pageSize"] == "100"
        if params.get("pageToken") == "p2":
            return json_response(200, {"files": [{"id": "b", "name": "Beta", "version": 7}]})
        return json_response(200, {"files": [{"id": "a", "name": "Alpha", "modifiedTime": "2026-09-06T10:00:00.000Z",
                                              "version": "3"}], "nextPageToken": "p2"})

    routes = {("GET", FILES): files,
              ("GET", f"{FILES}/a/export"): HttpResponse(200, (), "﻿Alpha text\r\nline 2\r\n".encode("utf-8")),
              ("GET", f"{FILES}/b/export"): HttpResponse(200, (), "café".encode("utf-8"))}
    remote, seen = store(routes, tmp_path, folder_id="fld")
    items = remote.list()
    assert [(i.id, i.version, i.record.name, i.record.text) for i in items] == [
        ("a", "3", "Alpha", "Alpha text\nline 2\n"), ("b", "7", "Beta", "café")]
    assert items[0].record.modified is not None and items[1].record.modified is None
    exports = [r for r in seen if "/export" in r.url]
    assert [query(r)["mimeType"] for r in exports] == ["text/plain", "text/plain"]
    assert all(r.header("Authorization") == "Bearer tok" for r in seen)


def test_create_uploads_multipart_related(tmp_path):
    def upload(request):
        assert query(request) == {"uploadType": "multipart", "fields": "id,version"}
        content_type = request.header("Content-Type")
        assert content_type.startswith("multipart/related; boundary=")
        boundary = content_type.split("boundary=", 1)[1]
        parts = request.body.split(f"--{boundary}".encode("ascii"))
        assert parts[0] == b"" and parts[-1] == b"--\r\n" and len(parts) == 4
        meta_head, meta_body = parts[1].split(b"\r\n\r\n", 1)
        assert b"Content-Type: application/json; charset=UTF-8" in meta_head
        assert json.loads(meta_body) == {"name": "Trip notes", "mimeType": DOC_MIME, "parents": ["fld"]}
        text_head, text_body = parts[2].split(b"\r\n\r\n", 1)
        assert b"Content-Type: text/plain; charset=UTF-8" in text_head
        assert text_body == "café\n\nline\r\n".encode("utf-8")
        return json_response(200, {"id": "new", "version": "1"})

    remote, seen = store({("POST", UPLOAD): upload}, tmp_path, folder_id="fld")
    assert remote.create(Document("Trip notes", "café\n\nline")) == "new"
    assert urlsplit(seen[0].url).netloc == "www.googleapis.com" and urlsplit(seen[0].url).path == UPLOAD
    body = multipart_related("b", {"name": "x"}, "t")
    assert body == b'--b\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{"name": "x"}\r\n--b\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\nt\r\n--b--\r\n'


def test_update_and_delete(tmp_path):
    def patch(request):
        assert query(request) == {"uploadType": "media", "fields": "id,version"}
        assert request.header("Content-Type") == "text/plain; charset=UTF-8"
        assert request.body == "new\ntext".encode("utf-8")
        return json_response(200, {"id": "a", "version": 9})

    routes = {("PATCH", f"{UPLOAD}/a"): patch, ("DELETE", f"{FILES}/a"): HttpResponse(204),
              ("DELETE", f"{FILES}/gone"): json_response(404, {}), ("DELETE", f"{FILES}/nope"): json_response(403, {"error": {}})}
    remote, seen = store(routes, tmp_path, folder_id="fld")
    assert remote.update("a", Document("A", "new\ntext")) == "9"
    remote.delete("a")
    remote.delete("gone")
    with pytest.raises(StoreError):
        remote.delete("nope")
    assert [r.method for r in seen] == ["PATCH", "DELETE", "DELETE", "DELETE"]


def test_failures_and_spec(tmp_path):
    remote, _ = store({("GET", FILES): json_response(403, {"error": {"message": "denied"}})}, tmp_path, folder_id="fld")
    with pytest.raises(StoreError) as info:
        remote.list()
    assert "403" in str(info.value) and "Bearer" not in str(info.value)
    remote, _ = store({("GET", FILES): json_response(200, {"files": [{"id": "a", "name": "A"}]})}, tmp_path, folder_id="fld")
    with pytest.raises(StoreError):
        remote.list()                                    # no export route → 404
    remote, _ = store({("POST", UPLOAD): json_response(200, {"version": "1"})}, tmp_path, folder_id="fld")
    with pytest.raises(StoreError):
        remote.create(Document("x", "y"))
    assert BACKEND.key == "gdocs" and BACKEND.title == "Google Docs" and BACKEND.login is not None
    assert "drive.file" in BACKEND.notes and SCOPES == ("https://www.googleapis.com/auth/drive.file",)
    assert [s.key for s in BACKEND.settings] == ["client_id", "client_secret", "token", "folder_id", "folder_name", "device_format"]
    assert query_literal("it's \\ done") == "it\\'s \\\\ done"
