"""End to end: a device folder ⇄ Word on OneDrive (an in-memory drive behind ``fake_transport``)
through the sync engine's ``plan`` / ``apply``."""
import hashlib
import time
from functools import partial
from typing import Dict, Tuple

import pytest

from jornada.rapi import RapiClient
from jornada.sendmirror import ENV_MIRROR_DIR
from jornada.sync.accounts import Account
from jornada.sync.documents import docx
from jornada.sync.documents.text_backends import DEVICE_STORE, WORD
from jornada.sync.engine import Options, Prefer, apply, plan, refresh_hashes
from jornada.sync.registry import BuildContext
from jornada.sync.state import SyncState
from jornada.webapi.http import HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY
from tests.fake_device import FakeFilesystem, FakeRapiServer

DEVICE_FOLDER = "\\My Documents"
DRIVE = "/v1.0/me/drive"
FOLDER = f"{DRIVE}/root:/Documents/Jornada:"


class FakeOneDrive:
    """item id → (name, bytes) served through fake_transport routes for one OneDrive folder."""

    def __init__(self, uploads=("Notes.docx",), pool=("d1", "d2", "d3", "d4")):
        self.files: Dict[str, Tuple[str, bytes]] = {}
        self._free = list(pool)
        routes = {("GET", f"{FOLDER}/children"): self._children}
        for item_id in pool:
            routes[("GET", f"{DRIVE}/items/{item_id}/content")] = partial(self._download, item_id)
            routes[("PUT", f"{DRIVE}/items/{item_id}/content")] = partial(self._replace, item_id)
            routes[("DELETE", f"{DRIVE}/items/{item_id}")] = partial(self._delete, item_id)
        for name in uploads:
            routes[("PUT", f"{DRIVE}/root:/Documents/Jornada/{name}:/content")] = partial(self._upload, name)
        self.transport, self.seen = fake_transport(routes)

    def put(self, name, text):
        item_id = self._free.pop(0)
        self.files[item_id] = (name, docx.from_text(text))
        return item_id

    def text(self, item_id):
        return docx.to_text(self.files[item_id][1])

    def _entry(self, item_id):
        name, data = self.files[item_id]
        return {"id": item_id, "name": name, "file": {}, "eTag": hashlib.md5(data).hexdigest(), "size": len(data),
                "lastModifiedDateTime": "2026-09-06T10:00:00Z"}

    def _children(self, _request):
        return json_response(200, {"value": [self._entry(item_id) for item_id in self.files]})

    def _download(self, item_id, _request):
        if item_id not in self.files:
            return json_response(404, {"error": {"code": "itemNotFound"}})
        return HttpResponse(200, (), self.files[item_id][1])

    def _replace(self, item_id, request):
        self.files[item_id] = (self.files[item_id][0], request.body)
        return json_response(200, self._entry(item_id))

    def _upload(self, name, request):
        item_id = self._free.pop(0)
        self.files[item_id] = (name, request.body)
        return json_response(201, self._entry(item_id))

    def _delete(self, item_id, _request):
        self.files.pop(item_id, None)
        return HttpResponse(204)


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.add(DEVICE_FOLDER)
    fs.files[DEVICE_FOLDER + "\\Notes.txt"] = b"hello from the device\r\n"
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


def test_device_folder_syncs_with_word_on_onedrive(device, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    drive = FakeOneDrive()
    report = drive.put("Report.docx", "Quarterly report\n\nAll good.")
    account = Account("docs", "documents", "word", (("client_id", "cid"),))
    logs = []
    context = BuildContext(log=logs.append, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           http_transport=drive.transport)
    remote = WORD.build(account, {TOKEN_KEY: Token("tok", "r", time.time() + 3600).to_dict()}, context)
    with RapiClient("127.0.0.1", device.port, timeout=5) as client:
        local = DEVICE_STORE(client, account, context)
        state = SyncState()

        # first sync: each side's document goes to the other, then nothing is left to do
        first = plan(local.list(), remote.list(), state)
        assert sorted(a.kind for a in first.actions) == ["create_local", "create_remote"]
        state, result = apply(first, local, remote, state, log=logs.append)
        assert not result.errors and result.counts == {"create_remote": 1, "create_local": 1}
        state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
        assert device.fs.files[DEVICE_FOLDER + "\\Report.txt"] == b"Quarterly report\r\n\r\nAll good."
        (notes_id,) = [item_id for item_id in drive.files if item_id != report]
        assert drive.files[notes_id][0] == "Notes.docx"
        assert drive.text(notes_id) == "hello from the device\n"      # CRLF on the device → a final newline
        assert (tmp_path / "mirror" / "My Documents" / "Report.txt").exists()
        assert plan(local.list(), remote.list(), state).is_empty

        # an edit in Word Online comes down; an edit on the device goes up
        drive.files[report] = ("Report.docx", docx.from_text("Quarterly report\nRevised."))
        state, result = apply(plan(local.list(), remote.list(), state), local, remote, state)
        assert result.counts == {"update_local": 1}
        assert device.fs.files[DEVICE_FOLDER + "\\Report.txt"] == b"Quarterly report\r\nRevised."
        device.fs.files[DEVICE_FOLDER + "\\Notes.txt"] = b"edited on the device"
        state, result = apply(plan(local.list(), remote.list(), state), local, remote, state)
        assert result.counts == {"update_remote": 1} and drive.text(notes_id) == "edited on the device"

        # both sides change: the preference decides; then a device deletion reaches OneDrive
        drive.files[report] = ("Report.docx", docx.from_text("cloud edit"))
        device.fs.files[DEVICE_FOLDER + "\\Report.txt"] = b"device edit"
        conflict = plan(local.list(), remote.list(), state, Options(prefer=Prefer.LOCAL))
        assert [a.kind for a in conflict.actions] == ["update_remote"]
        state, result = apply(conflict, local, remote, state)
        assert not result.errors and drive.text(report) == "device edit"
        del device.fs.files[DEVICE_FOLDER + "\\Notes.txt"]
        state, result = apply(plan(local.list(), remote.list(), state), local, remote, state)
        assert result.counts == {"delete_remote": 1} and notes_id not in drive.files
        assert plan(local.list(), remote.list(), state).is_empty and len(state.links) == 1
    assert not any("Bearer" in line or "tok" == line for line in logs)
    assert all(r.header("Authorization") == "Bearer tok" for r in drive.seen)
