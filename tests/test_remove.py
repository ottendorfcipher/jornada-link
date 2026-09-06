import pytest

from jornada.rapi import RapiClient, RapiError
from jornada.remove import remove_tree
from tests.fake_device import FakeFilesystem, FakeRapiServer


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.update({
        "\\Temp", "\\Temp\\RestoreTest", "\\Temp\\RestoreTest\\Sub",
        "\\Temp\\RestoreTest\\Sub\\Deep", "\\Keep",
    })
    fs.files["\\Temp\\RestoreTest\\a.txt"] = b"a"
    fs.files["\\Temp\\RestoreTest\\Sub\\b.bin"] = bytes(50)
    fs.files["\\Temp\\RestoreTest\\Sub\\Deep\\c.dat"] = bytes(10)
    fs.files["\\Keep\\keep.txt"] = b"keep"
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


def _client(server):
    return RapiClient("127.0.0.1", server.port, timeout=5)


def test_remove_tree_deletes_everything_depth_first(device):
    with _client(device) as client:
        stats = remove_tree(client, "\\Temp\\RestoreTest", log=lambda _l: None)
    assert stats.files_deleted == 3
    assert stats.directories_deleted == 3   # RestoreTest, Sub, Deep
    assert not stats.errors
    assert not any(p.startswith("\\Temp\\RestoreTest") for p in device.fs.files)
    assert not any(d.startswith("\\Temp\\RestoreTest") for d in device.fs.dirs)
    # siblings and parents untouched
    assert "\\Temp" in device.fs.dirs
    assert device.fs.files["\\Keep\\keep.txt"] == b"keep"


def test_remove_single_file(device):
    with _client(device) as client:
        stats = remove_tree(client, "\\Keep\\keep.txt", log=lambda _l: None)
    assert stats.files_deleted == 1 and stats.directories_deleted == 0
    assert "\\Keep\\keep.txt" not in device.fs.files
    assert "\\Keep" in device.fs.dirs   # containing dir left alone


def test_remove_refuses_root(device):
    with _client(device) as client:
        for target in ("\\", "", "  "):
            with pytest.raises(ValueError):
                remove_tree(client, target, log=lambda _l: None)


def test_remove_missing_path_raises(device):
    with _client(device) as client:
        with pytest.raises(RapiError):
            remove_tree(client, "\\Temp\\NoSuchThing", log=lambda _l: None)


def test_remove_normalizes_trailing_slash(device):
    with _client(device) as client:
        stats = remove_tree(client, "\\Temp\\RestoreTest\\", log=lambda _l: None)
    assert stats.directories_deleted == 3
