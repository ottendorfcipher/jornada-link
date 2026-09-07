from pathlib import Path

import pytest

from jornada import usb_doctor as d
from jornada import usb_profiles as p
from jornada.usb_registry import SerialNode, UsbDevice, UsbInterface


def device(vid, pid, product="", serial="", nodes=(), ifaces=((0xFF, 0xFF),), location=1, vendor="V"):
    return UsbDevice(
        vendor_id=vid, product_id=pid, vendor=vendor, product=product, serial=serial, location_id=location,
        device_class=0,
        interfaces=tuple(UsbInterface(i, cls, sub, 0, None) for i, (cls, sub) in enumerate(ifaces)),
        serial_nodes=tuple(SerialNode(*n) for n in nodes))


APPLE = "com.apple.DriverKit-AppleUSBFTDI"
VENDOR = "com.ftdi.vcp.dext"
FTDI_TWO_NODES = device(0x0403, 0x6001, "FT232R USB UART", "AB", nodes=(
    ("/dev/cu.usbserial-AB", VENDOR, False), ("/dev/cu.usbserial-3", APPLE, True)))
SH3 = p.handheld("jornada-680e")
ARM = p.handheld("jornada-720")


def titles(diag, level=None):
    return [f.title for f in diag.findings if level is None or f.level == level]


def test_ranking_prefers_apple_driver_and_openable_nodes():
    ranked = d.rank_candidates((FTDI_TWO_NODES,))
    assert [c.path for c in ranked] == ["/dev/cu.usbserial-3", "/dev/cu.usbserial-AB"]
    assert ranked[0].score > ranked[1].score
    assert d.recommend_serial_path((FTDI_TWO_NODES,)) == "/dev/cu.usbserial-3"
    prolific = device(0x067B, 0x2303, "USB-Serial Controller", nodes=(("/dev/cu.usbserial-110", "com.prolific.cdc", True),), location=2)
    dock = device(0x0403, 0x6015, "Jornada Dock Bridge", "JDOCK1", nodes=(("/dev/cu.usbserial-JDOCK1", APPLE, True),), location=3)
    ranked = d.rank_candidates((prolific, FTDI_TWO_NODES, dock))
    assert ranked[0].path == "/dev/cu.usbserial-JDOCK1" and ranked[0].classification.role == p.ROLE_DOCK_BRIDGE
    assert ranked[-1].path == "/dev/cu.usbserial-AB"


def test_pinned_port_wins_when_present_and_warns_when_missing():
    diag = d.diagnose((FTDI_TWO_NODES,), pinned="/dev/cu.usbserial-AB")
    assert diag.recommended == "/dev/cu.usbserial-AB"
    assert any("pinned" in t for t in titles(diag, d.LEVEL_OK))
    missing = d.diagnose((FTDI_TWO_NODES,), pinned="/dev/cu.usbserial-GONE")
    assert missing.recommended == "/dev/cu.usbserial-3"
    assert "Pinned serial port /dev/cu.usbserial-GONE is not present" in titles(missing, d.LEVEL_WARN)


def test_no_adapter_is_an_error_and_worst_level():
    diag = d.diagnose(())
    assert diag.recommended is None
    assert titles(diag, d.LEVEL_ERROR) == ["No USB-serial adapter found"]
    assert diag.worst_level == d.LEVEL_ERROR
    assert d.diagnose((FTDI_TWO_NODES,)).worst_level == d.LEVEL_WARN   # the unopenable node


def test_sh3_handheld_gets_the_dock_explanation_or_the_bridge_ok():
    diag = d.diagnose((FTDI_TWO_NODES,), handheld=SH3)
    inert = [f for f in diag.findings if "inert" in f.title]
    assert inert and inert[0].level == d.LEVEL_INFO and "SH7709A" in inert[0].detail
    dock = device(0x0403, 0x6001, "Jornada Dock Bridge", "JDOCK1", nodes=(("/dev/cu.usbserial-JDOCK1", APPLE, True),))
    bridged = d.diagnose((dock,), handheld=SH3)
    assert any("linked through the dock's USB jack" in t for t in titles(bridged, d.LEVEL_OK))
    assert not any("inert" in t for t in titles(bridged))


def test_arm_handheld_and_sync_devices():
    sync = device(0x03F0, 0x2016, "HP USB Sync", location=5)
    diag = d.diagnose((sync,), handheld=ARM)
    assert diag.recommended is None
    assert not titles(diag, d.LEVEL_ERROR)          # a sync device present is not "no adapter"
    assert any("can enumerate on the dock's USB jack" in t for t in titles(diag, d.LEVEL_INFO))
    assert any(t.startswith("Windows CE USB Sync device present (03f0:2016") for t in titles(diag))
    confused = d.diagnose((sync, FTDI_TWO_NODES), handheld=SH3)
    assert "That USB Sync device is not the SH-3 Jornada" in titles(confused, d.LEVEL_WARN)


def test_bridge_without_a_node_names_the_missing_driver():
    prolific = device(0x067B, 0x2303, "USB-Serial Controller")
    diag = d.diagnose((prolific,))
    errors = [f for f in diag.findings if f.level == d.LEVEL_ERROR]
    assert any("has no serial node" in f.title and "Prolific" in f.detail for f in errors)
    ftdi = device(0x0403, 0x6001, "FT232R")
    diag = d.diagnose((ftdi,))
    assert any("created one by itself" in f.detail for f in diag.findings if f.level == d.LEVEL_ERROR)


def test_two_nodes_are_explained_and_unknown_devices_ignored():
    other = device(0x2537, 0x1081, "NS1081", ifaces=((8, 6),), location=9)
    diag = d.diagnose((FTDI_TWO_NODES, other))
    assert any("exposes 2 serial nodes" in t for t in titles(diag, d.LEVEL_INFO))
    assert [i.role for i in diag.devices] == [p.ROLE_SERIAL_BRIDGE, None]
    assert any("is root-only" in t for t in titles(diag, d.LEVEL_WARN))


def test_pin_file_round_trip(tmp_path: Path):
    pin = tmp_path / "state" / "serial"
    assert d.read_pin(pin) is None
    assert d.write_pin("/dev/cu.usbserial-3", pin) == pin
    assert d.read_pin(pin) == "/dev/cu.usbserial-3"
    with pytest.raises(ValueError):
        d.write_pin("/dev/ttys001", pin)
    with pytest.raises(ValueError):
        d.write_pin("/dev/cu.usbserial-3; rm -rf /", pin)
    pin.write_text("/dev/cu.bad path\n")
    assert d.read_pin(pin) is None
    assert d.clear_pin(pin) is True and d.clear_pin(pin) is False


def test_serial_path_rule_matches_the_root_helper():
    assert d.is_valid_serial_path("/dev/cu.usbserial-BG00T191")
    assert d.is_valid_serial_path("/dev/cu.SLAB_USBtoUART")
    assert not d.is_valid_serial_path("/dev/tty.usbserial-1")
    assert not d.is_valid_serial_path("/dev/cu.usbserial-1 x")
    assert not d.is_valid_serial_path("/dev/cu.")


def test_handheld_from_dccm_state():
    assert d.handheld_from_state(None) is None
    assert d.handheld_from_state({"device": "nope"}) is None
    state = {"device": {"name": "Jornada680", "hardware": "SH3"}, "ip": "192.168.131.201"}
    assert d.handheld_from_state(state).key == "jornada-680"
    assert d.handheld_from_state({"device": {"hardware": "StrongARM"}}).key == "sa1110-hpc2000-family"
