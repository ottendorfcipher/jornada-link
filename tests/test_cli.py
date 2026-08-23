from pathlib import Path

import pytest

from jornada import cli
from tests.fake_device import FakeFilesystem, FakeRapiServer


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.add("\\My Documents")
    fs.files["\\My Documents\\a.txt"] = b"A" * 10
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


def run(device, *argv):
    return cli.main(["--ip", "127.0.0.1", "--rapi-port", str(device.port), *argv])


def test_ls_and_status(device, capsys):
    assert run(device, "ls", "\\My Documents") == 0
    out = capsys.readouterr().out
    assert "a.txt" in out and "1 item(s)" in out
    assert run(device, "status") == 0
    out = capsys.readouterr().out
    assert "Windows CE 2.11" in out and "object store" in out


def test_get_put_rm_roundtrip(device, tmp_path: Path, capsys):
    local = tmp_path / "a.txt"
    assert run(device, "get", "\\My Documents\\a.txt", str(local)) == 0
    assert local.read_bytes() == b"A" * 10
    src = tmp_path / "b.bin"
    src.write_bytes(b"B" * 3000)
    assert run(device, "put", str(src), "\\My Documents\\") == 0
    assert device.fs.files["\\My Documents\\b.bin"] == b"B" * 3000
    assert run(device, "mv", "\\My Documents\\b.bin", "\\My Documents\\c.bin") == 0
    assert run(device, "rm", "\\My Documents\\c.bin") == 0
    assert "\\My Documents\\c.bin" not in device.fs.files
    assert run(device, "mkdir", "\\My Documents\\d") == 0
    assert run(device, "rmdir", "\\My Documents\\d") == 0
    assert run(device, "run", "\\Windows\\pword.exe") == 0
    assert device.launched == [("\\Windows\\pword.exe", None)]


def test_rapi_error_is_reported_not_raised(device, capsys):
    assert run(device, "rm", "\\missing") == 1
    assert "error:" in capsys.readouterr().err


def test_unreachable_device_gives_hint():
    with pytest.raises(SystemExit) as info:
        cli.main(["--ip", "127.0.0.1", "--rapi-port", "1", "ls"])
    assert "PPP link" in str(info.value)


def test_put_missing_local_file(device, tmp_path):
    with pytest.raises(SystemExit):
        run(device, "put", str(tmp_path / "nope"))


def test_dccm_port_in_use_is_reported():
    import socket
    import threading
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    outcome = {}

    def attempt():
        try:
            cli.main(["dccm", "--port", str(port)])
        except SystemExit as exc:
            outcome["exit"] = str(exc)

    thread = threading.Thread(target=attempt, daemon=True)
    try:
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "dccm bound a port that was already in use"
        assert "cannot listen" in outcome["exit"]
    finally:
        blocker.close()
