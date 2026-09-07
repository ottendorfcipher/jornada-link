import plistlib
import subprocess
from pathlib import Path

import pytest

from jornada import usb_registry as r

FIXTURE = Path(__file__).parent / "fixtures" / "ioreg-usb-ftdi.plist"


def load_fixture():
    with open(FIXTURE, "rb") as handle:
        return plistlib.load(handle)


def apple_only_writable(path: str) -> bool:
    return path.endswith("usbserial-3")


def test_parse_fixture_finds_the_ftdi_adapter_and_both_nodes():
    devices = r.parse_registry(load_fixture(), access=apple_only_writable)
    ftdi = next(d for d in devices if d.vid_pid == "0403:6001")
    assert ftdi.vendor == "FTDI" and ftdi.product == "FT232R USB UART" and ftdi.serial == "FT0000A1"
    assert [n.path for n in ftdi.serial_nodes] == ["/dev/cu.usbserial-3", "/dev/cu.usbserial-FT0000A1"]
    drivers = {n.path: n.driver for n in ftdi.serial_nodes}
    assert drivers["/dev/cu.usbserial-3"] == "com.apple.DriverKit-AppleUSBFTDI"
    assert drivers["/dev/cu.usbserial-FT0000A1"] == "com.ftdi.vcp.dext"
    assert {n.path: n.writable for n in ftdi.serial_nodes} == {
        "/dev/cu.usbserial-3": True, "/dev/cu.usbserial-FT0000A1": False}
    assert ftdi.interfaces[0].class_code == 0xFF and not ftdi.is_hub
    assert ftdi.interface_classes == ((0xFF, 0xFF),)


def test_hubs_are_flagged_and_filtered():
    devices = r.parse_registry(load_fixture(), access=apple_only_writable)
    hubs = [d for d in devices if d.is_hub]
    assert {d.product for d in hubs} == {"USB2.0 Hub", "USB3.0 Hub"}
    assert all(not d.serial_nodes for d in hubs)
    non_hubs = r.usb_devices(devices)
    assert {d.vid_pid for d in non_hubs} == {"0403:6001", "291a:8346", "2537:1081"}
    assert devices == tuple(sorted(devices, key=lambda d: (d.location_id, d.vid_pid)))


def synthetic_tree():
    inner_serial = {"IOObjectClass": "IOSerialBSDClient", "IOCalloutDevice": "/dev/cu.usbserial-inner",
                    "IORegistryEntryName": "IOSerialBSDClient"}
    inner_driver = {"IOObjectClass": "IOUserSerial", "CFBundleIdentifier": "com.example.serial",
                    "IORegistryEntryName": "ExampleSerial", "IORegistryEntryChildren": [inner_serial]}
    inner_iface = {"IOObjectClass": "IOUSBHostInterface", "bInterfaceNumber": 1, "bInterfaceClass": 2,
                   "bInterfaceSubClass": 2, "bInterfaceProtocol": 1, "IORegistryEntryName": "if",
                   "IORegistryEntryChildren": [inner_driver]}
    inner = {"IOObjectClass": "IOUSBHostDevice", "IORegistryEntryID": 2, "idVendor": 0x1234, "idProduct": 1,
             "IORegistryEntryName": "Inner", "locationID": 20, "bDeviceClass": 0,
             "IORegistryEntryChildren": [inner_iface]}
    port = {"IOObjectClass": "AppleUSB20HubPort", "IORegistryEntryName": "port",
            "IORegistryEntryChildren": [inner]}
    hub = {"IOObjectClass": "IOUSBHostDevice", "IORegistryEntryID": 1, "idVendor": 0x2109, "idProduct": 5,
           "IORegistryEntryName": "Hub", "locationID": 10, "bDeviceClass": 9,
           "IORegistryEntryChildren": [{"IOObjectClass": "AppleUSB20Hub", "IORegistryEntryName": "drv",
                                        "IORegistryEntryChildren": [port]}]}
    return [hub, {"not": "a dict entry"}, 42]


def test_nested_devices_keep_their_own_nodes_and_ids_dedupe():
    tree = synthetic_tree()
    devices = r.parse_registry(tree + tree, access=lambda _p: True)   # same entries twice
    assert [d.product for d in devices] == ["Hub", "Inner"]
    hub, inner = devices
    assert hub.is_hub and hub.serial_nodes == ()
    assert inner.serial_nodes == (r.SerialNode("/dev/cu.usbserial-inner", "com.example.serial", True),)
    assert inner.interfaces == (r.UsbInterface(1, 2, 2, 1, "com.example.serial"),)
    assert inner.label == "Inner" and inner.vid_pid == "1234:0001"


def test_read_registry_runs_ioreg_and_parses(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=FIXTURE.read_bytes(), stderr=b"")

    devices = r.read_registry(run=fake_run, access=apple_only_writable)
    assert calls == [[r.IOREG, *r.IOREG_ARGS]]
    assert any(d.vid_pid == "0403:6001" for d in devices)


def test_read_registry_error_paths():
    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(r.RegistryError):
        r.read_registry(run=missing)

    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"boom")

    with pytest.raises(r.RegistryError, match="status 1"):
        r.read_registry(run=failing)

    def garbage(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=b"<not a plist", stderr=b"")

    with pytest.raises(r.RegistryError, match="property list"):
        r.read_registry(run=garbage)

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(r.RegistryError):
        r.read_registry(run=slow)
