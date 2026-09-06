from pathlib import Path

import pytest

from jornada.backup import backup_tree
from jornada.rapi import RapiClient
from jornada.restore import free_space_warning, is_archived_version, plan_size, restore_tree
from tests.fake_device import FakeFilesystem, FakeRapiServer


@pytest.fixture
def populated_device():
    fs = FakeFilesystem()
    fs.dirs.update({"\\My Documents", "\\My Documents\\Sub", "\\Windows"})
    fs.files["\\My Documents\\a.txt"] = b"alpha"
    fs.files["\\My Documents\\Sub\\b.bin"] = bytes(range(200))
    fs.files["\\Windows\\w.txt"] = b"win"
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def blank_device():
    server = FakeRapiServer(FakeFilesystem()).start()
    try:
        yield server
    finally:
        server.stop()


def _client(server):
    return RapiClient("127.0.0.1", server.port, timeout=5)


def test_backup_then_restore_round_trip(populated_device, blank_device, tmp_path: Path):
    with _client(populated_device) as source:
        backup_tree(source, "\\", tmp_path / "bk", log=lambda _line: None)
    with _client(blank_device) as target:
        stats = restore_tree(target, tmp_path / "bk", log=lambda _line: None)
    assert stats.files_sent == 3 and not stats.errors
    assert blank_device.fs.files["\\My Documents\\a.txt"] == b"alpha"
    assert blank_device.fs.files["\\My Documents\\Sub\\b.bin"] == bytes(range(200))
    assert blank_device.fs.files["\\Windows\\w.txt"] == b"win"
    assert "\\My Documents\\Sub" in blank_device.fs.dirs
    # manifest must not have been pushed to the device
    assert not any("manifest" in path.lower() for path in blank_device.fs.files)


def test_restore_skips_matching_files_unless_forced(populated_device, tmp_path: Path):
    with _client(populated_device) as client:
        backup_tree(client, "\\", tmp_path / "bk", log=lambda _line: None)
    with _client(populated_device) as client:
        again = restore_tree(client, tmp_path / "bk", log=lambda _line: None)
        assert again.files_sent == 0 and again.skipped_existing == 3
        forced = restore_tree(client, tmp_path / "bk", force=True, log=lambda _line: None)
        assert forced.files_sent == 3


def test_restore_excludes_archived_versions_and_junk(blank_device, tmp_path: Path):
    tree = tmp_path / "mirror"
    (tree / "My Documents").mkdir(parents=True)
    (tree / "My Documents" / "doc.pwd").write_bytes(b"current")
    (tree / "My Documents" / "doc.20260906-013045.pwd").write_bytes(b"old")
    (tree / "My Documents" / "doc.20260906-013045-1.pwd").write_bytes(b"older")
    (tree / "sent-manifest.jsonl").write_text("{}\n")
    (tree / "backup-manifest.json").write_text("{}")
    (tree / ".DS_Store").write_bytes(b"junk")
    with _client(blank_device) as client:
        stats = restore_tree(client, tree, log=lambda _line: None)
    assert stats.files_sent == 1 and stats.skipped_archived == 2
    assert set(blank_device.fs.files) == {"\\My Documents\\doc.pwd"}


def test_restore_into_nested_destination(blank_device, tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "x.txt").write_bytes(b"x")
    with _client(blank_device) as client:
        stats = restore_tree(client, tree, "\\My Documents\\Restored", log=lambda _line: None)
    assert stats.files_sent == 1
    assert blank_device.fs.files["\\My Documents\\Restored\\x.txt"] == b"x"
    assert "\\My Documents\\Restored" in blank_device.fs.dirs


def test_dry_run_touches_nothing(blank_device, tmp_path: Path):
    tree = tmp_path / "tree"
    (tree / "Sub").mkdir(parents=True)
    (tree / "Sub" / "f.bin").write_bytes(bytes(50))
    with _client(blank_device) as client:
        stats = restore_tree(client, tree, dry_run=True, log=lambda _line: None)
    assert stats.files_sent == 1 and stats.bytes_sent == 50
    assert blank_device.fs.files == {}
    assert blank_device.fs.dirs == {"\\"}


def test_plan_size_and_archived_detection(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(bytes(10))
    (tmp_path / "a.20260906-013045.txt").write_bytes(bytes(99))
    (tmp_path / ".DS_Store").write_bytes(bytes(7))
    assert plan_size(tmp_path) == 10
    assert is_archived_version("doc.20260906-013045.pwd")
    assert is_archived_version("doc.20260906-013045-2.pwd")
    assert is_archived_version("noext.20260906-013045")
    assert not is_archived_version("doc.pwd")
    assert not is_archived_version("report-2026.txt")


def test_free_space_warning(populated_device, tmp_path: Path):
    with _client(populated_device) as client:
        assert free_space_warning(client, 1) is None
        warning = free_space_warning(client, 10**9)
        assert warning is not None and "free" in warning
