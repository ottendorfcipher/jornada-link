from pathlib import Path

import pytest

from jornada.backup import backup_tree
from jornada.constants import FILE_ATTRIBUTE_INROM
from jornada.rapi import RapiClient
from tests.fake_device import FakeFilesystem, FakeRapiServer


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.update({"\\My Documents", "\\My Documents\\Sub", "\\Windows"})
    fs.files["\\My Documents\\a.txt"] = b"alpha"
    fs.files["\\My Documents\\Sub\\b.bin"] = bytes(300)
    fs.files["\\Windows\\rom.dll"] = b"rom!"
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


def test_backup_tree_mirrors_and_manifests(device, tmp_path: Path):
    with RapiClient("127.0.0.1", device.port, timeout=5) as client:
        stats = backup_tree(client, "\\", tmp_path / "bk", log=lambda _line: None)
    assert (tmp_path / "bk/My Documents/a.txt").read_bytes() == b"alpha"
    assert (tmp_path / "bk/My Documents/Sub/b.bin").read_bytes() == bytes(300)
    assert stats.files == 3 and not stats.errors
    manifest = (tmp_path / "bk/backup-manifest.json").read_text()
    assert "\\\\My Documents\\\\Sub\\\\b.bin" in manifest

    # second run: everything unchanged
    with RapiClient("127.0.0.1", device.port, timeout=5) as client:
        stats2 = backup_tree(client, "\\", tmp_path / "bk", log=lambda _line: None)
    assert stats2.files == 0 and stats2.skipped_existing == 3


def test_backup_rejects_traversal_names(tmp_path: Path):
    from jornada.backup import _local_name, _safe_child
    assert _local_name("..") == "_.."
    assert _local_name("a/b") == "a_b"
    assert _local_name("a\\b") == "a_b"
    assert _local_name("") == "_unnamed"
    root = tmp_path / "root"
    root.mkdir()
    # a normal name resolves under root; a traversal token is neutralized, not escaping
    assert _safe_child(root, "file.txt").name == "file.txt"
    assert _safe_child(root, "..").parent == root  # neutralized to "_.." under root
