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


def test_doctor_text_output(bus, capsys):
    assert cli.main(["usb"]) == 0
    out = capsys.readouterr().out
    assert "handheld: unknown" in out
    assert "0403:6001  FTDI FT232R USB UART" in out
    assert "/dev/cu.usbserial-3  driver com.apple.DriverKit-AppleUSBFTDI  (selected)" in out
    assert "not openable" in out and "root-only" in out and "WARN" in out and "OK  " in out


def test_doctor_with_model_and_json(bus, capsys):
    assert cli.main(["usb", "doctor", "--model", "jornada-680e", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["handheld"] == "jornada-680e"
    assert data["recommended"] == "/dev/cu.usbserial-3"
    assert any("inert" in f["title"] for f in data["findings"])
    ftdi = next(dev for dev in data["devices"] if dev["vid_pid"] == "0403:6001")
    assert ftdi["profile"] == "ftdi-ft232r" and len(ftdi["serial_nodes"]) == 2
    assert cli.main(["usb", "--model", "jornada-720"]) == 0
    assert "dock USB jack live" in capsys.readouterr().out


def test_unknown_model_is_rejected(bus):
    with pytest.raises(SystemExit, match="unknown handheld model"):
        cli.main(["usb", "--model", "jornada-9000"])


def test_list_and_pick(bus, capsys):
    assert cli.main(["usb", "list"]) == 0
    out = capsys.readouterr().out
    assert "FTDI FT232R" in out and "OK" not in out
    assert cli.main(["usb", "pick"]) == 0
    assert capsys.readouterr().out.strip() == "/dev/cu.usbserial-3"


def test_pick_and_doctor_without_adapters(bus, monkeypatch, capsys):
    monkeypatch.setattr(usb_cli, "read_registry", lambda: ())
    assert cli.main(["usb", "pick"]) == 1
    assert "no USB-serial adapter" in capsys.readouterr().err
    assert cli.main(["usb"]) == 1
    assert "No USB-serial adapter found" in capsys.readouterr().out
    assert cli.main(["usb", "list"]) == 0


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


def test_profiles_dump_matches_the_table(bus, capsys):
    assert cli.main(["usb", "profiles"]) == 0
    assert json.loads(capsys.readouterr().out) == usb_profiles.table()


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
