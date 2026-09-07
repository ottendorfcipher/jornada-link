from datetime import datetime

import pytest

from jornada.pim.filestore import DeviceFolderStore
from jornada.pim.models import Document
from jornada.rapi import RapiClient
from jornada.sendmirror import ENV_MIRROR_DIR
from jornada.sync.accounts import Account, AccountError
from jornada.sync.documents import MODULE, rtf, textcodec
from jornada.sync.registry import BuildContext
from tests.fake_device import FakeFilesystem, FakeRapiServer

FOLDER = "\\My Documents"


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.add(FOLDER)
    fs.files[FOLDER + "\\Ideas.txt"] = "héllo\r\nworld\r\n".encode("cp1252")
    fs.files[FOLDER + "\\Memo.rtf"] = rtf.from_text("Dear all,\n\nSee attached — café.")
    fs.files[FOLDER + "\\Broken.rtf"] = b"this is not rtf"
    fs.files[FOLDER + "\\photo.bmp"] = b"BM"
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as c:
        yield c


def context(tmp_path, log=None):
    return BuildContext(log=log or (lambda _l: None), sync_dir=tmp_path, save_secrets=lambda _c: None)


def account(**settings):
    return Account("docs", "documents", "word", tuple(sorted(settings.items())))


def test_codec_decodes_txt_and_rtf():
    record = textcodec.decode("Ideas.txt", "a\r\nb".encode("cp1252"), 1_700_000_000.0)
    assert record == Document("Ideas", "a\nb", "text", datetime.fromtimestamp(1_700_000_000.0))
    memo = textcodec.decode("Memo.RTF", rtf.from_text("x\ny"), None)
    assert memo.name == "Memo" and memo.text == "x\ny" and memo.modified is None
    assert textcodec.decode("noext", b"plain", None).name == "noext"
    with pytest.raises(ValueError):
        textcodec.decode("bad.rtf", b"nope", None)


def test_codec_encodes_in_the_device_format():
    doc = Document("Trip: notes/2026", "café\nline 2")
    assert textcodec.encode(doc) == ("Trip_ notes_2026.txt", "café\r\nline 2".encode("cp1252"))
    name, data = textcodec.encode(doc, "rtf")
    assert name == "Trip_ notes_2026.rtf" and rtf.to_text(data) == "café\nline 2"
    with pytest.raises(AccountError):
        textcodec.codec_for("pwd")
    assert textcodec.codec_for("txt").extensions == (".txt", ".rtf")


def test_device_store_lists_creates_updates_deletes(client, device, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    logs = []
    store = textcodec.device_store(client, account(), context(tmp_path, logs.append))
    assert isinstance(store, DeviceFolderStore) and store.folder == FOLDER
    items = store.list()
    assert [i.id for i in items] == ["Ideas.txt", "Memo.rtf"]
    assert items[0].record.name == "Ideas" and items[0].record.text == "héllo\nworld\n"
    assert isinstance(items[0].record.modified, datetime) and items[0].version
    assert items[1].record.text == "Dear all,\n\nSee attached — café." and items[1].record.kind == "text"
    assert any("Broken.rtf" in line for line in logs)
    name = store.create(Document("Report", "one\ntwo"))
    assert name == "Report.txt" and device.fs.files[FOLDER + "\\Report.txt"] == b"one\r\ntwo"
    assert (tmp_path / "mirror" / "My Documents" / "Report.txt").read_bytes() == b"one\r\ntwo"
    assert store.create(Document("Report", "another")) == "Report-2.txt"
    store.update("Ideas.txt", Document("Ideas", "changed"))
    assert device.fs.files[FOLDER + "\\Ideas.txt"] == b"changed"
    store.delete("Memo.rtf")
    assert FOLDER + "\\Memo.rtf" not in device.fs.files


def test_device_store_writes_rtf_when_asked(client, device, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    store = textcodec.device_store(client, account(device_format="RTF", folder="\\Storage Card\\Docs"),
                                   context(tmp_path))
    assert store.folder == "\\Storage Card\\Docs" and store.list() == ()
    assert store.create(Document("Ideas", "café €\n日")) == "Ideas.rtf"
    data = device.fs.files["\\Storage Card\\Docs\\Ideas.rtf"]
    assert data.startswith(b"{\\rtf1") and rtf.to_text(data) == "café €\n日"
    store.update("Ideas.rtf", Document("Ideas", "second"))
    assert rtf.to_text(device.fs.files["\\Storage Card\\Docs\\Ideas.rtf"]) == "second"
    with pytest.raises(AccountError):
        textcodec.device_store(client, account(device_format="pwd"), context(tmp_path))


def test_module_assembles_the_text_backends(client, tmp_path):
    keys = [backend.key for backend in MODULE.backends]
    assert [k for k in keys if k in ("word", "gdocs", "pages")] == ["word", "gdocs", "pages"]
    for key in ("word", "gdocs", "pages"):
        spec = MODULE.backend(key)
        assert "device_format" in [s.key for s in spec.settings] and spec.build is not None
        store = MODULE.device_store(client, Account("a", "documents", key), context(tmp_path))
        assert isinstance(store, DeviceFolderStore) and store.folder == FOLDER
    assert MODULE.backend("word").login is not None and MODULE.backend("pages").login is None


def test_remote_time_stamps():
    assert textcodec.remote_modified("2026-09-06T10:00:00.1234567Z") is not None
    assert textcodec.remote_modified("2026-09-06T10:00:00Z").tzinfo is None
    assert textcodec.remote_modified("2026-09-06") is None
    assert textcodec.remote_modified("") is None and textcodec.remote_modified(None) is None
    assert textcodec.remote_modified("not a time") is None
