import pytest

from jornada.constants import FILE_ATTRIBUTE_ARCHIVE, FILE_ATTRIBUTE_DIRECTORY
from jornada.rapi import RapiClient, RapiError
from tests.fake_device import FakeFilesystem, FakeRapiServer


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.update({"\\My Documents", "\\Windows"})
    fs.files["\\My Documents\\notes.txt"] = b"hello jornada\n"
    fs.files["\\Windows\\big.bin"] = bytes(range(256)) * 100
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as c:
        yield c


def test_listdir_root_and_subdir(client):
    names = {(e.name, e.is_dir) for e in client.listdir("\\")}
    assert names == {("My Documents", True), ("Windows", True)}
    docs = client.listdir("\\My Documents")
    assert [(e.name, e.size, e.attributes) for e in docs] == [("notes.txt", 14, FILE_ATTRIBUTE_ARCHIVE)]
    assert docs[0].mtime is not None


def test_download_small_and_chunked(client):
    assert client.download("\\My Documents\\notes.txt") == b"hello jornada\n"
    chunks = list(client.iter_download("\\Windows\\big.bin", chunk=1000))
    assert len(chunks) == 26
    assert b"".join(chunks) == bytes(range(256)) * 100


def test_download_missing_file_raises(client):
    with pytest.raises(RapiError) as info:
        client.download("\\nope.txt")
    assert info.value.last_error == 2


def test_upload_roundtrip_with_progress(client, device):
    payload = bytes(i % 251 for i in range(20000))
    seen = []
    written = client.upload("\\My Documents\\up.bin", payload, chunk=4096, progress=lambda d, t: seen.append((d, t)))
    assert written == 20000
    assert device.fs.files["\\My Documents\\up.bin"] == payload
    assert seen[-1] == (20000, 20000) and len(seen) == 5
    assert client.download("\\My Documents\\up.bin") == payload


def test_upload_empty_file(client, device):
    assert client.upload("\\My Documents\\empty", b"") == 0
    assert device.fs.files["\\My Documents\\empty"] == b""


def test_upload_into_missing_directory_fails(client):
    with pytest.raises(RapiError):
        client.upload("\\Nope\\x.txt", b"x")


def test_mkdir_mv_rm_rmdir(client, device):
    client.create_directory("\\My Documents\\new")
    assert "\\My Documents\\new" in device.fs.dirs
    client.move_file("\\My Documents\\notes.txt", "\\My Documents\\new\\notes.txt")
    assert client.get_file_attributes("\\My Documents\\new\\notes.txt") == FILE_ATTRIBUTE_ARCHIVE
    assert client.get_file_attributes("\\My Documents\\new") == FILE_ATTRIBUTE_DIRECTORY
    assert client.get_file_attributes("\\gone") is None
    with pytest.raises(RapiError):
        client.remove_directory("\\My Documents\\new")  # not empty
    client.delete_file("\\My Documents\\new\\notes.txt")
    client.remove_directory("\\My Documents\\new")
    assert "\\My Documents\\new" not in device.fs.dirs
    with pytest.raises(RapiError):
        client.delete_file("\\My Documents\\new\\notes.txt")


def test_create_process_and_system_info(client, device):
    pid = client.create_process("\\Windows\\pword.exe", "\\My Documents\\notes.txt")
    assert pid == 0x1001
    assert device.launched == [("\\Windows\\pword.exe", "\\My Documents\\notes.txt")]
    version = client.get_version()
    assert (version.major, version.minor, version.build) == (2, 11, 11171)
    store = client.get_store_information()
    assert store.free_size == 9 * 1024 * 1024
    power = client.get_power_status()
    assert power.battery_percent == 77 and power.ac_line_status == 1


def test_unknown_command_raises(client):
    with pytest.raises(RapiError):
        client.call(0x7F)


def test_password_protected_device():
    server = FakeRapiServer(password="1234", key=0x42).start()
    try:
        ok = RapiClient("127.0.0.1", server.port, timeout=5)
        ok.connect(password="1234", key=0x42)
        ok.close()
        bad = RapiClient("127.0.0.1", server.port, timeout=5)
        with pytest.raises(RapiError):
            bad.connect(password="wrong", key=0x42)
    finally:
        server.stop()


def test_not_connected():
    with pytest.raises(RapiError):
        RapiClient("127.0.0.1", 1).call(0)


def test_sync_time(client, device):
    import time
    client.sync_time_from_mac(now=1_700_000_000.5)
    assert device.clock_set_to == [1_700_000_000.5]
    client.sync_time_from_mac()
    assert abs(device.clock_set_to[-1] - time.time()) < 5
