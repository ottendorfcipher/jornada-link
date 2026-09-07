import json
from datetime import datetime

import pytest

from jornada.pim.filestore import DeviceFolderStore
from jornada.pim.models import Note
from jornada.rapi import RapiClient
from jornada.sendmirror import ENV_MIRROR_DIR, MANIFEST_NAME
from jornada.sync import registry
from jornada.sync.accounts import Account
from jornada.sync.engine import Options, Prefer, apply, plan, refresh_hashes
from jornada.sync.notes import DEFAULT_FOLDER, MODULE, TXT_CODEC, decode_txt, device_store, encode_txt
from jornada.sync.registry import BuildContext
from jornada.sync.state import SyncState
from tests.fake_device import FakeFilesystem, FakeRapiServer

NOTES = "\\My Documents\\Notes"


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.update({"\\My Documents", NOTES})
    fs.files[NOTES + "\\Ideas.txt"] = "one\r\ntwo\r\n".encode("cp1252")
    fs.files[NOTES + "\\Todo.txt"] = b"call mum"
    fs.files[NOTES + "\\photo.bmp"] = b"BM"
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as connection:
        yield connection


def context(tmp_path, lines=None):
    log = lines.append if lines is not None else (lambda _line: None)
    return BuildContext(log=log, sync_dir=tmp_path / "sync", save_secrets=lambda _changes: None)


def test_module_spec_and_registry():
    assert MODULE.key == "notes" and MODULE.title == "Notes" and not MODULE.is_bridge
    assert [backend.key for backend in MODULE.backends] == ["apple", "logseq", "bear"]
    assert [(s.key, s.required, s.default) for s in MODULE.settings] == [("folder", False, DEFAULT_FOLDER)]
    assert DEFAULT_FOLDER == "\\My Documents\\Notes"
    assert registry.load_modules({"notes": "jornada.sync.notes"})["notes"] is MODULE
    assert MODULE.backend("logseq").missing_settings(Account("a", "notes", "logseq"), {}) == ("graph",)
    for backend in MODULE.backends:       # no backend reuses the module's device-folder key
        assert "folder" not in {s.key for s in backend.settings}


def test_txt_codec():
    note = decode_txt("Ideas.txt", "h\xe9llo\r\nworld".encode("cp1252"), 1_000_000_000.0)
    assert note == Note("Ideas", "héllo\nworld", modified=datetime(2001, 9, 9, 1, 46, 40))
    assert decode_txt("NOTE.TXT", b"\xef\xbb\xbfbom", None) == Note("NOTE", "bom")
    assert encode_txt(Note("Bad/name?", "a\nb")) == ("Bad_name_.txt", b"a\r\nb")
    assert TXT_CODEC.extensions == (".txt",)


def test_device_store_lists_and_writes_txt_files(client, device, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    account = Account("hand", "notes", "logseq", (("graph", "/x"),))
    store = device_store(client, account, context(tmp_path))
    assert isinstance(store, DeviceFolderStore) and store.folder == DEFAULT_FOLDER
    items = store.list()
    assert [(i.id, i.record.title, i.record.body) for i in items] == [("Ideas.txt", "Ideas", "one\ntwo\n"),
                                                                       ("Todo.txt", "Todo", "call mum")]
    assert items[0].record.modified is not None and items[0].record.folder == ""
    assert store.create(Note("Shopping", "milk\nbread", folder="pages")) == "Shopping.txt"
    assert device.fs.files[NOTES + "\\Shopping.txt"] == b"milk\r\nbread"
    assert (tmp_path / "mirror" / "My Documents" / "Notes" / "Shopping.txt").read_bytes() == b"milk\r\nbread"
    manifest = (tmp_path / "mirror" / MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
    assert json.loads(manifest[-1])["source"] == "sync:hand"
    lines = []
    custom = device_store(client, account.with_setting("folder", "\\Storage Card\\Jot"), context(tmp_path, lines))
    assert custom.folder == "\\Storage Card\\Jot" and custom.list() == ()
    assert "\\Storage Card\\Jot" in device.fs.dirs and any("created" in line for line in lines)


def test_engine_round_trip_between_the_device_folder_and_logseq(client, device, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    graph = tmp_path / "graph"
    (graph / "pages").mkdir(parents=True)
    (graph / "pages" / "Ideas.md").write_text("one\ntwo\n", encoding="utf-8")       # same as the device: paired
    (graph / "pages" / "Shopping.md").write_text("- milk\n", encoding="utf-8")       # only in Logseq
    account = Account("hand", "notes", "logseq", (("graph", str(graph)),))
    ctx = context(tmp_path)
    local = device_store(client, account, ctx)
    remote = MODULE.backend(account.backend).build(account, {}, ctx)

    first = plan(local.list(), remote.list(), SyncState())
    kinds = {(a.kind, a.local_id, a.remote_id) for a in first.actions}
    assert {("link", "Ideas.txt", "pages/Ideas.md"), ("create_remote", "Todo.txt", None),
            ("create_local", None, "pages/Shopping.md")} <= kinds
    state, result = apply(first, local, remote, SyncState())
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    assert (graph / "pages" / "Todo.md").read_text(encoding="utf-8") == "call mum"
    assert device.fs.files[NOTES + "\\Shopping.txt"] == b"- milk\r\n"
    assert {l.local_id: l.remote_id for l in state.links} == {"Ideas.txt": "pages/Ideas.md", "Todo.txt": "pages/Todo.md",
                                                             "Shopping.txt": "pages/Shopping.md"}
    assert plan(local.list(), remote.list(), state).is_empty

    device.fs.files[NOTES + "\\Todo.txt"] = b"call mum\r\ncall dad"                   # edited on the device
    second = plan(local.list(), remote.list(), state)
    assert [(a.kind, a.remote_id) for a in second.actions] == [("update_remote", "pages/Todo.md")]
    state, result = apply(second, local, remote, state)
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    assert (graph / "pages" / "Todo.md").read_text(encoding="utf-8") == "call mum\ncall dad"

    (graph / "pages" / "Shopping.md").write_text("- milk\n- eggs\n", encoding="utf-8")   # edited in Logseq
    (graph / "pages" / "Ideas.md").unlink()                                              # deleted in Logseq
    third = plan(local.list(), remote.list(), state)
    assert sorted(a.kind for a in third.actions) == ["delete_local", "update_local"]
    state, result = apply(third, local, remote, state)
    assert not result.errors
    state = refresh_hashes(state, local.list(), remote.list(), result.touched_local, result.touched_remote)
    assert device.fs.files[NOTES + "\\Shopping.txt"] == b"- milk\r\n- eggs\r\n"
    assert NOTES + "\\Ideas.txt" not in device.fs.files
    assert plan(local.list(), remote.list(), state).is_empty

    device.fs.files[NOTES + "\\Shopping.txt"] = b"device"                                # both sides edited
    (graph / "pages" / "Shopping.md").write_text("logseq", encoding="utf-8")
    device_wins = plan(local.list(), remote.list(), state, Options(prefer=Prefer.LOCAL))
    assert [a.kind for a in device_wins.actions] == ["update_remote"]
    state, result = apply(device_wins, local, remote, state)
    assert not result.errors and (graph / "pages" / "Shopping.md").read_text(encoding="utf-8") == "device"
