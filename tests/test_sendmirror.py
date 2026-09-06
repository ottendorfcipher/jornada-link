import json
from pathlib import Path

import pytest

from jornada.sendmirror import ENV_MIRROR_DIR, MANIFEST_NAME, mirror_root, mirror_sent


def manifest_lines(root: Path):
    text = (root / MANIFEST_NAME).read_text()
    return [json.loads(line) for line in text.splitlines() if line]


def test_mirror_creates_device_shaped_tree(tmp_path: Path):
    target = mirror_sent(b"hello", "\\My Documents\\notes.txt", root=tmp_path, source="/tmp/notes.txt")
    assert target == tmp_path / "My Documents" / "notes.txt"
    assert target.read_bytes() == b"hello"
    lines = manifest_lines(tmp_path)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["device_path"] == "\\My Documents\\notes.txt"
    assert entry["size"] == 5
    assert entry["md5"] == "5d41402abc4b2a76b9719d911017c592"
    assert entry["unchanged"] is False
    assert entry["source"] == "/tmp/notes.txt"


def test_identical_resend_dedupes_but_still_logs(tmp_path: Path):
    mirror_sent(b"same", "\\My Documents\\a.txt", root=tmp_path)
    mirror_sent(b"same", "\\My Documents\\a.txt", root=tmp_path)
    files = [p.name for p in (tmp_path / "My Documents").iterdir()]
    assert files == ["a.txt"]  # no timestamped sibling
    lines = manifest_lines(tmp_path)
    assert [line["unchanged"] for line in lines] == [False, True]


def test_changed_resend_archives_previous_version(tmp_path: Path):
    mirror_sent(b"version one", "\\My Documents\\doc.pwd", root=tmp_path, now=1_700_000_000)
    mirror_sent(b"version two", "\\My Documents\\doc.pwd", root=tmp_path, now=1_700_000_060)
    directory = tmp_path / "My Documents"
    names = sorted(p.name for p in directory.iterdir())
    assert "doc.pwd" in names
    archived = [n for n in names if n.startswith("doc.") and n.endswith(".pwd") and n != "doc.pwd"]
    assert len(archived) == 1, names
    assert (directory / "doc.pwd").read_bytes() == b"version two"
    assert (directory / archived[0]).read_bytes() == b"version one"


def test_same_second_archives_do_not_collide(tmp_path: Path):
    for index in range(3):
        mirror_sent(f"v{index}".encode(), "\\Temp\\x.bin", root=tmp_path, now=1_700_000_000)
    files = list((tmp_path / "Temp").iterdir())
    assert len(files) == 3  # canonical + two distinct archived names
    assert len({p.name for p in files}) == 3


def test_hostile_device_paths_stay_inside_root(tmp_path: Path):
    target = mirror_sent(b"x", "\\..\\..\\evil.txt", root=tmp_path)
    assert tmp_path.resolve() in target.resolve().parents
    target2 = mirror_sent(b"y", "\\My Documents\\..\\up.txt", root=tmp_path)
    assert tmp_path.resolve() in target2.resolve().parents
    outside = [p for p in tmp_path.parent.iterdir() if p.name in ("evil.txt", "up.txt")]
    assert outside == []


def test_env_override_controls_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "elsewhere"))
    assert mirror_root() == tmp_path / "elsewhere"
    monkeypatch.delenv(ENV_MIRROR_DIR)
    assert "Jornada Backup" in str(mirror_root())


def test_empty_device_path_is_handled(tmp_path: Path):
    target = mirror_sent(b"data", "\\", root=tmp_path)
    assert target.parent == tmp_path
    assert target.exists()
