from pathlib import Path

import pytest

from jornada.pim.filestore import DeviceFolderStore, FileCodec
from jornada.pim.models import Note
from jornada.pim.textfiles import decode_text, encode_text, safe_filename, unique_filename
from jornada.rapi import RapiClient
from jornada.sendmirror import ENV_MIRROR_DIR
from jornada.sync.base import StoreError
from tests.fake_device import FakeFilesystem, FakeRapiServer


def note_codec() -> FileCodec:
    return FileCodec(
        extensions=(".txt",),
        decode=lambda name, data, mtime: Note(title=name[:-4], body=decode_text(data)),
        encode=lambda note: (safe_filename(note.title, ".txt"), encode_text(note.body)),
    )


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.update({"\\My Documents", "\\My Documents\\Notes"})
    fs.files["\\My Documents\\Notes\\Ideas.txt"] = "one\r\ntwo\r\n".encode("cp1252")
    fs.files["\\My Documents\\Notes\\photo.bmp"] = b"BM"
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as c:
        yield c


def test_list_create_update_delete(client, device, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    store = DeviceFolderStore(client, "\\My Documents\\Notes", note_codec(), source="test")
    (item,) = store.list()
    assert item.id == "Ideas.txt" and item.record == Note("Ideas", "one\ntwo\n")
    name = store.create(Note("Ideas", "another"))
    assert name == "Ideas-2.txt" and device.fs.files["\\My Documents\\Notes\\Ideas-2.txt"] == b"another"
    assert (tmp_path / "mirror" / "My Documents" / "Notes" / "Ideas-2.txt").exists()
    store.update("Ideas.txt", Note("Renamed", "changed\n"))
    assert device.fs.files["\\My Documents\\Notes\\Ideas.txt"] == b"changed\r\n"
    store.delete("Ideas-2.txt")
    assert "\\My Documents\\Notes\\Ideas-2.txt" not in device.fs.files
    with pytest.raises(StoreError):
        store.delete("..\\evil.txt")
    with pytest.raises(StoreError):
        store.update("sub\\x.txt", Note("x", "y"))


def test_folder_is_created_when_missing(client, device, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    store = DeviceFolderStore(client, "\\My Documents\\Logseq\\journals", note_codec(), mirror=False)
    assert store.list() == ()
    assert "\\My Documents\\Logseq\\journals" in device.fs.dirs
    store.create(Note("2026_09_07", "- hello"))
    assert device.fs.files["\\My Documents\\Logseq\\journals\\2026_09_07.txt"] == b"- hello"


def test_text_helpers():
    assert decode_text("héllo\r\n".encode("cp1252")) == "héllo\n"
    assert decode_text("héllo".encode("utf-8")) == "héllo"
    assert decode_text(b"\xef\xbb\xbfbom") == "bom"
    assert decode_text("wide".encode("utf-16")) == "wide"
    assert decode_text("w\x00i\x00d\x00e\x00".encode("latin-1")) == "wide"
    assert encode_text("a\nb\r\nc\r") == b"a\r\nb\r\nc\r\n"
    assert safe_filename('  Bad/name:*?"<>| ', ".txt") == "Bad_name_______.txt"
    assert safe_filename("", ".txt") == "Untitled.txt" and safe_filename("x" * 100, ".md").endswith(".md")
    assert len(safe_filename("x" * 100, ".md")) == 63
    assert unique_filename("a.txt", {"A.TXT", "a-2.txt"}) == "a-3.txt"
    assert unique_filename("noext", {"noext"}) == "noext-2"
