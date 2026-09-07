"""Enumerate USB devices on macOS through the I/O Registry, standard library only.

``ioreg -a -r -c IOUSBHostDevice -l`` prints every USB device as a plist subtree
(IOService plane), which carries the interfaces below the device and — for a
USB-serial bridge — the ``IOSerialBSDClient`` entries whose ``IOCalloutDevice``
is the ``/dev/cu.*`` node the PPP link opens. One physical adapter can expose
more than one node when both Apple's built-in driver and a vendor DriverKit
extension bind to it; each node is reported with the driver that owns it and
whether this user can open it.

:func:`parse_registry` is pure (it takes the decoded plist) so the tests feed it
captured trees; :func:`read_registry` runs ``ioreg``.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .usb_profiles import USB_CLASS_HUB

IOREG = "/usr/sbin/ioreg"
IOREG_ARGS: Tuple[str, ...] = ("-a", "-r", "-c", "IOUSBHostDevice", "-l", "-w0")
IOREG_TIMEOUT_S = 15.0

CLASS_DEVICE = "IOUSBHostDevice"
CLASS_INTERFACE = "IOUSBHostInterface"
CLASS_SERIAL_CLIENT = "IOSerialBSDClient"


class RegistryError(Exception):
    """``ioreg`` could not be run or its output was not a plist."""


@dataclass(frozen=True)
class SerialNode:
    """A ``/dev/cu.*`` callout node and the driver that created it."""

    path: str
    driver: str
    writable: bool


@dataclass(frozen=True)
class UsbInterface:
    number: int
    class_code: int
    subclass: int
    protocol: int
    driver: Optional[str]


@dataclass(frozen=True)
class UsbDevice:
    vendor_id: int
    product_id: int
    vendor: str
    product: str
    serial: str
    location_id: int
    device_class: int
    interfaces: Tuple[UsbInterface, ...]
    serial_nodes: Tuple[SerialNode, ...]

    @property
    def is_hub(self) -> bool:
        return self.device_class == USB_CLASS_HUB or any(
            i.class_code == USB_CLASS_HUB for i in self.interfaces)

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"

    @property
    def interface_classes(self) -> Tuple[Tuple[int, int], ...]:
        return tuple((i.class_code, i.subclass) for i in self.interfaces)

    @property
    def label(self) -> str:
        parts = [self.vendor.strip(), self.product.strip()]
        return " ".join(p for p in parts if p) or self.vid_pid


def _children(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    kids = node.get("IORegistryEntryChildren")
    return [k for k in kids if isinstance(k, dict)] if isinstance(kids, list) else []


def _int(node: Dict[str, Any], key: str, default: int = 0) -> int:
    value = node.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _text(node: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _driver_of(node: Dict[str, Any]) -> Optional[str]:
    """The bundle identifier of the driver entry (kernel or DriverKit) at ``node``."""
    return _text(node, "CFBundleIdentifier", "IOUserServerName") or None


def _collect_below(node: Dict[str, Any], parent_driver: Optional[str],
                   interfaces: List[UsbInterface], serial_nodes: List[SerialNode],
                   access: Callable[[str], bool]) -> None:
    """Gather interfaces and tty nodes below a device, stopping at nested USB devices."""
    for child in _children(node):
        cls = _text(child, "IOObjectClass")
        if cls == CLASS_DEVICE:
            continue                       # a device behind a hub: handled by the outer walk
        driver = _driver_of(child) or parent_driver
        if cls == CLASS_INTERFACE:
            driver_below = next((_driver_of(k) for k in _children(child) if _driver_of(k)), None)
            interfaces.append(UsbInterface(
                number=_int(child, "bInterfaceNumber"),
                class_code=_int(child, "bInterfaceClass"),
                subclass=_int(child, "bInterfaceSubClass"),
                protocol=_int(child, "bInterfaceProtocol"),
                driver=driver_below,
            ))
        elif cls == CLASS_SERIAL_CLIENT:
            path = _text(child, "IOCalloutDevice")
            if path:
                serial_nodes.append(SerialNode(path=path, driver=parent_driver or "unknown",
                                               writable=access(path)))
        _collect_below(child, driver, interfaces, serial_nodes, access)


def _device_from(node: Dict[str, Any], access: Callable[[str], bool]) -> UsbDevice:
    interfaces: List[UsbInterface] = []
    serial_nodes: List[SerialNode] = []
    _collect_below(node, None, interfaces, serial_nodes, access)
    return UsbDevice(
        vendor_id=_int(node, "idVendor"),
        product_id=_int(node, "idProduct"),
        vendor=_text(node, "USB Vendor Name", "kUSBVendorString"),
        product=_text(node, "USB Product Name", "kUSBProductString", "IORegistryEntryName"),
        serial=_text(node, "USB Serial Number", "kUSBSerialNumberString"),
        location_id=_int(node, "locationID"),
        device_class=_int(node, "bDeviceClass"),
        interfaces=tuple(sorted(interfaces, key=lambda i: i.number)),
        serial_nodes=tuple(sorted(serial_nodes, key=lambda n: n.path)),
    )


def _walk(node: Any, seen: Dict[int, UsbDevice], access: Callable[[str], bool]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, seen, access)
        return
    if not isinstance(node, dict):
        return
    if _text(node, "IOObjectClass") == CLASS_DEVICE:
        entry_id = _int(node, "IORegistryEntryID", default=id(node))
        if entry_id not in seen:
            seen[entry_id] = _device_from(node, access)
    for child in _children(node):
        _walk(child, seen, access)


def _writable(path: str) -> bool:
    return os.access(path, os.R_OK | os.W_OK)


def parse_registry(root: Any, access: Callable[[str], bool] = _writable) -> Tuple[UsbDevice, ...]:
    """Every ``IOUSBHostDevice`` in a decoded ``ioreg`` plist, hubs included, in bus order."""
    seen: Dict[int, UsbDevice] = {}
    _walk(root, seen, access)
    return tuple(sorted(seen.values(), key=lambda d: (d.location_id, d.vid_pid)))


def read_registry(run: Callable[..., "subprocess.CompletedProcess[bytes]"] = subprocess.run,
                  access: Callable[[str], bool] = _writable) -> Tuple[UsbDevice, ...]:
    """Run ``ioreg`` and parse it; raises :class:`RegistryError` when that is impossible."""
    try:
        completed = run([IOREG, *IOREG_ARGS], capture_output=True, timeout=IOREG_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RegistryError(f"cannot run {IOREG}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip() if completed.stderr else ""
        raise RegistryError(f"{IOREG} exited with status {completed.returncode}: {detail}")
    try:
        root = plistlib.loads(completed.stdout or b"")
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise RegistryError(f"{IOREG} output was not a property list: {exc}") from exc
    return parse_registry(root, access)


def usb_devices(devices: Tuple[UsbDevice, ...]) -> Tuple[UsbDevice, ...]:
    """The devices that are not hubs (hubs never carry a link)."""
    return tuple(d for d in devices if not d.is_hub)
