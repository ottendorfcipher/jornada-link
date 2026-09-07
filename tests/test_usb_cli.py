import json
import plistlib
from pathlib import Path

import pytest

from jornada import cli, usb_cli, usb_profiles
from jornada import usb_registry as r

FIXTURE = Path(__file__).parent / "fixtures" / "ioreg-usb-ftdi.plist"


@pytest.fixture
def bus(monkeypatch, tmp_path):
    """The captured FTDI bus, a private HOME for the pin file, and no dccm session."""
    with open(FIXTURE, "rb") as handle:
        devices = r.parse_registry(plistlib.load(handle), access=lambda path: path.endswith("-3"))
    monkeypatch.setattr(usb_cli, "read_registry", lambda: devices)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(usb_cli, "read_state", lambda _path: None)
    return devices


def test_status_is_condensed_and_auto_detected(bus, capsys):
    assert cli.main(["usb"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("adapter : FTDI FT232R USB UART")
    assert lines[1].startswith("port    : /dev/cu.usbserial-3")
    assert "AppleUSBFTDI" in lines[1]
    # only real problems are shown — no per-node tables, no OK/info chatter
    assert "OK  " not in out and "info" not in out
    assert "(selected)" not in out
    assert len(lines) <= 6


def test_status_json_shape(bus, capsys):
    assert cli.main(["usb", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["adapter"]["vid_pid"] == "0403:6001"
    assert data["adapter"]["profile"] == "ftdi-ft232r"
    assert data["recommended"] == "/dev/cu.usbserial-3"
    assert data["handheld"] is None            # nothing connected, nothing to configure
    assert all(f["level"] in ("warn", "error") for f in data["findings"])


def test_no_adapter_is_an_error(bus, monkeypatch, capsys):
    monkeypatch.setattr(usb_cli, "read_registry", lambda: ())
    assert cli.main(["usb"]) == 1
    out = capsys.readouterr().out
    assert "adapter : none" in out and "port    : none" in out
    assert cli.main(["usb", "pick"]) == 1
    assert "no USB-serial adapter" in capsys.readouterr().err


def test_pick_prints_only_the_port(bus, capsys):
    assert cli.main(["usb", "pick"]) == 0
    assert capsys.readouterr().out.strip() == "/dev/cu.usbserial-3"


def test_pin_unpin_round_trip(bus, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(usb_cli, "_node_exists", lambda path: path.endswith("-FT0000A1"))
    assert cli.main(["usb", "pin", "/dev/cu.usbserial-FT0000A1"]) == 0
    assert (tmp_path / ".jornada-link" / "serial").read_text() == "/dev/cu.usbserial-FT0000A1\n"
    assert cli.main(["usb", "pick"]) == 0
    assert capsys.readouterr().out.strip().endswith("/dev/cu.usbserial-FT0000A1")
    assert cli.main(["usb", "unpin"]) == 0
    assert not (tmp_path / ".jornada-link" / "serial").exists()
    assert cli.main(["usb", "unpin"]) == 0
    assert "no serial port was pinned" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="only plain /dev/cu"):
        cli.main(["usb", "pin", "/dev/ttys000"])
    with pytest.raises(SystemExit, match="not an attached"):
        cli.main(["usb", "pin", "/dev/cu.usbserial-3"])
    with pytest.raises(SystemExit, match="usage"):
        cli.main(["usb", "pin"])


def test_removed_knobs_are_gone(bus):
    # The surface is status/pick/pin/unpin only; no model picker, no dumps.
    for argv in (["usb", "list"], ["usb", "profiles"], ["usb", "doctor"], ["usb", "--model", "jornada-680e"]):
        with pytest.raises(SystemExit):
            cli.main(argv)


def test_registry_failure_is_explained(bus, monkeypatch):
    def broken():
        raise r.RegistryError("ioreg exploded")

    monkeypatch.setattr(usb_cli, "read_registry", broken)
    with pytest.raises(SystemExit, match="ioreg exploded"):
        cli.main(["usb"])


def test_parity_checker_accepts_python_table(tmp_path, capsys):
    from tests import check_usb_parity
    good = tmp_path / "good.json"
    good.write_text(usb_profiles.table_json())
    assert check_usb_parity.main(str(good)) == 0
    table = usb_profiles.table()
    table["drivers"][0]["chip"] = "changed"
    del table["handhelds"][-1]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(table))
    assert check_usb_parity.main(str(bad)) == 1
    out = capsys.readouterr().out
    assert "differs in chip" in out and "missing from Swift" in out
