"""The USB link table: driver profiles for USB devices that can carry a Jornada
link, and the capability matrix of the handhelds that fit the HP F1822A dock.

Two questions this module answers without touching hardware:

* "What is this USB device, and how could it connect a Jornada?" — a
  USB-to-RS-232 bridge (FTDI, Prolific, Silicon Labs, WCH, CDC-ACM) gives us a
  ``/dev/cu.*`` node for the serial PPP path; a Windows CE "USB Sync" function
  device is a handheld speaking USB natively (StrongARM Jornada 720/728 and the
  Pocket PCs), which macOS has no driver for.
* "Can this handheld use the dock's USB-B jack at all?" — only if the
  handheld's own silicon has a USB device controller. The SH-3 Jornada 680/690
  family (SH7709A + HD64461 companion chip) has none, so the dock's USB pins
  are inert for it; the SA-1110 in the 710/720/728 has one built in.

One table, two languages: ``macapp/Sources/JornadaCore/UsbProfiles.swift`` must
agree with this module — ``tests/check_usb_parity.py`` compares the Swift dump
against :func:`table_json`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, Optional, Tuple

# --- Roles: what a device can do for the link ------------------------------
ROLE_SERIAL_BRIDGE = "serial-bridge"      # USB-to-RS-232 chip: gives a /dev/cu.* node
ROLE_DOCK_BRIDGE = "dock-bridge"          # a serial bridge built into the dock (recognised by name)
ROLE_WINCE_USB_SYNC = "wince-usb-sync"    # a CE handheld's own USB function ("Windows CE USB Devices")

# --- macOS driver availability ---------------------------------------------
DRIVER_BUILT_IN = "built-in"              # macOS ships the driver; the tty node appears by itself
DRIVER_VENDOR = "vendor-extension"        # the chip vendor's DriverKit extension is needed
DRIVER_NONE = "none"                      # no macOS driver exists for this function

# --- Handheld USB client silicon -------------------------------------------
USB_CLIENT_NONE = "none"                  # no USB device controller anywhere on the board
USB_CLIENT_SA1110 = "sa1110-udc"          # StrongARM SA-1110 integrated USB device controller

ARCH_SH3 = "SH3"
ARCH_ARM = "ARM"

# A serial bridge whose USB product or serial string carries one of these
# markers is treated as a bridge retrofitted into the dock (see docs/usb-link.md).
DOCK_MARKERS: Tuple[str, ...] = ("JORNADA", "JDOCK")

USB_CLASS_CDC = 0x02
USB_SUBCLASS_ACM = 0x02
USB_CLASS_HUB = 0x09


@dataclass(frozen=True)
class UsbDriverProfile:
    """One recognised USB vendor/product (``product_id`` None = any product of the vendor)."""

    key: str
    vendor_id: Optional[int]
    product_id: Optional[int]
    name: str
    chip: str
    role: str
    macos_driver: str
    driver_name: str
    notes: str

    @property
    def vid_pid(self) -> str:
        vid = "----" if self.vendor_id is None else f"{self.vendor_id:04x}"
        pid = "----" if self.product_id is None else f"{self.product_id:04x}"
        return f"{vid}:{pid}"


@dataclass(frozen=True)
class HandheldModel:
    """A handheld that physically fits the dock, and whether its USB pins are live."""

    key: str
    name: str
    cpu: str
    architecture: str
    os: str
    usb_client: str
    dock_usb: bool
    notes: str


@dataclass(frozen=True)
class Classification:
    profile: UsbDriverProfile
    role: str


def _ftdi(pid: int, key: str, name: str, chip: str, notes: str = "") -> UsbDriverProfile:
    return UsbDriverProfile(key, 0x0403, pid, name, chip, ROLE_SERIAL_BRIDGE, DRIVER_BUILT_IN,
                            "AppleUSBFTDI", notes)


def _prolific(pid: int, key: str, name: str, chip: str, notes: str = "") -> UsbDriverProfile:
    return UsbDriverProfile(key, 0x067B, pid, name, chip, ROLE_SERIAL_BRIDGE, DRIVER_VENDOR,
                            "Prolific PL2303 DriverKit extension", notes)


def _silabs(pid: int, key: str, name: str, chip: str) -> UsbDriverProfile:
    return UsbDriverProfile(key, 0x10C4, pid, name, chip, ROLE_SERIAL_BRIDGE, DRIVER_BUILT_IN,
                            "AppleUSBSLCOM", "Older macOS releases need Silicon Labs' VCP driver instead.")


def _wch(pid: int, key: str, name: str, chip: str) -> UsbDriverProfile:
    return UsbDriverProfile(key, 0x1A86, pid, name, chip, ROLE_SERIAL_BRIDGE, DRIVER_VENDOR,
                            "WCH CH34x VCP driver",
                            "Recent macOS releases may bind a built-in driver; otherwise install WCH's.")


def _hp_sync(pid: int) -> UsbDriverProfile:
    return UsbDriverProfile(
        f"hp-usb-sync-{pid:04x}", 0x03F0, pid, f"HP USB Sync (product {pid:04x})",
        "SA-1110 UDC or Pocket PC USB function", ROLE_WINCE_USB_SYNC, DRIVER_NONE,
        "wceusbsh.sys (Windows) / ipaq (Linux)",
        "Listed in Microsoft's wceusbsh.inf and the Linux ipaq driver; the Jornada 720/728 "
        "cradle and the 540/560 Pocket PCs enumerate as one of these.")


def _wince_sync(vid: int, pid: int, key: str, name: str) -> UsbDriverProfile:
    return UsbDriverProfile(key, vid, pid, name, "Windows CE USB function", ROLE_WINCE_USB_SYNC,
                            DRIVER_NONE, "wceusbsh.sys (Windows) / ipaq (Linux)",
                            "Listed in Microsoft's wceusbsh.inf and the Linux ipaq driver.")


CDC_ACM_PROFILE = UsbDriverProfile(
    "usb-cdc-acm", None, None, "USB CDC-ACM serial function", "CDC-ACM", ROLE_SERIAL_BRIDGE,
    DRIVER_BUILT_IN, "AppleUSBACM", "Class-compliant; matched by interface class, not vendor/product.")

DRIVER_PROFILES: Tuple[UsbDriverProfile, ...] = (
    _ftdi(0x6001, "ftdi-ft232r", "FTDI FT232R / FT232BM / FT245", "FT232R",
          "The adapter jornada-link was developed and tested with."),
    _ftdi(0x6015, "ftdi-ft-x", "FTDI FT231X / FT230X / FT234XD", "FT-X"),
    _ftdi(0x6010, "ftdi-ft2232", "FTDI FT2232C/D/H (two ports)", "FT2232"),
    _ftdi(0x6011, "ftdi-ft4232h", "FTDI FT4232H (four ports)", "FT4232H"),
    _ftdi(0x6014, "ftdi-ft232h", "FTDI FT232H", "FT232H"),
    _prolific(0x2303, "prolific-pl2303", "Prolific PL2303 (HX/HXD/TA/TB and clones)", "PL2303",
              "Prolific's current driver refuses counterfeit and end-of-life HXA/XA parts."),
    _prolific(0x23A3, "prolific-pl2303gc", "Prolific PL2303GC", "PL2303GC"),
    _prolific(0x23B3, "prolific-pl2303gb", "Prolific PL2303GB", "PL2303GB"),
    _prolific(0x23C3, "prolific-pl2303gt", "Prolific PL2303GT", "PL2303GT"),
    _prolific(0x23D3, "prolific-pl2303gl", "Prolific PL2303GL", "PL2303GL"),
    _prolific(0x23E3, "prolific-pl2303ge", "Prolific PL2303GE", "PL2303GE"),
    _prolific(0x23F3, "prolific-pl2303gs", "Prolific PL2303GS", "PL2303GS"),
    _silabs(0xEA60, "silabs-cp210x", "Silicon Labs CP2102 / CP2102N / CP2104", "CP210x"),
    _silabs(0xEA70, "silabs-cp2105", "Silicon Labs CP2105 (two ports)", "CP2105"),
    _silabs(0xEA71, "silabs-cp2108", "Silicon Labs CP2108 (four ports)", "CP2108"),
    _wch(0x7523, "wch-ch340", "WCH CH340 / CH340G / CH340C", "CH340"),
    _wch(0x5523, "wch-ch341", "WCH CH341 (serial mode)", "CH341"),
    _wch(0x55D4, "wch-ch9102", "WCH CH9102", "CH9102"),
    _hp_sync(0x1016), _hp_sync(0x1116), _hp_sync(0x1216),
    _hp_sync(0x2016), _hp_sync(0x2116), _hp_sync(0x2216),
    _hp_sync(0x3016), _hp_sync(0x3116), _hp_sync(0x3216),
    _hp_sync(0x4016), _hp_sync(0x4116), _hp_sync(0x4216),
    _hp_sync(0x5016), _hp_sync(0x5116), _hp_sync(0x5216),
    _wince_sync(0x049F, 0x0003, "compaq-ipaq-sync", "Compaq iPAQ USB Sync"),
    _wince_sync(0x045E, 0x00CE, "microsoft-wince-sync", "Microsoft Windows CE USB Sync"),
    _wince_sync(0x0BB4, 0x00CE, "htc-wince-sync", "HTC Windows CE USB Sync"),
)

_BY_VID_PID: Dict[Tuple[int, Optional[int]], UsbDriverProfile] = {
    (p.vendor_id, p.product_id): p for p in DRIVER_PROFILES if p.vendor_id is not None
}

_SH3_NOTE = ("Hitachi SH7709A + HD64461 companion chip: no USB device controller on the board, "
             "so the dock's USB-B jack carries nothing for this model. Serial (RS-232) only.")
_ARM_NOTE = ("StrongARM SA-1110 with an integrated USB device controller (UDC); enumerates on the "
             "dock's USB-B jack as a Windows CE USB Sync device.")

HANDHELD_MODELS: Tuple[HandheldModel, ...] = (
    HandheldModel("jornada-680", "HP Jornada 680", "Hitachi SH7709A (SH-3) 133 MHz", ARCH_SH3,
                  "Windows CE 2.11 / H/PC Pro 3.0", USB_CLIENT_NONE, False, _SH3_NOTE),
    HandheldModel("jornada-680e", "HP Jornada 680e", "Hitachi SH7709A (SH-3) 133 MHz", ARCH_SH3,
                  "Windows CE 2.11 / H/PC Pro 3.0", USB_CLIENT_NONE, False, _SH3_NOTE),
    HandheldModel("jornada-690", "HP Jornada 690", "Hitachi SH7709A (SH-3) 133 MHz", ARCH_SH3,
                  "Windows CE 2.11 / H/PC Pro 3.01", USB_CLIENT_NONE, False, _SH3_NOTE),
    HandheldModel("jornada-690e", "HP Jornada 690e", "Hitachi SH7709A (SH-3) 133 MHz", ARCH_SH3,
                  "Windows CE 2.11 / H/PC Pro 3.01", USB_CLIENT_NONE, False, _SH3_NOTE),
    HandheldModel("jornada-710", "HP Jornada 710", "Intel StrongARM SA-1110 206 MHz", ARCH_ARM,
                  "Windows CE 3.0 / H/PC 2000", USB_CLIENT_SA1110, True, _ARM_NOTE),
    HandheldModel("jornada-720", "HP Jornada 720", "Intel StrongARM SA-1110 206 MHz", ARCH_ARM,
                  "Windows CE 3.0 / H/PC 2000", USB_CLIENT_SA1110, True, _ARM_NOTE),
    HandheldModel("jornada-728", "HP Jornada 728", "Intel StrongARM SA-1110 206 MHz", ARCH_ARM,
                  "Windows CE 3.0 / H/PC 2000", USB_CLIENT_SA1110, True, _ARM_NOTE),
    # Family rows: what we can say when only the CPU architecture is known
    # (the dccm handshake reports "SH3" / "StrongARM", not the model number).
    HandheldModel("sh3-hpc-pro-family", "SH3 Handheld PC Pro (Jornada 680/680e/690/690e)",
                  "Hitachi SH7709A (SH-3) 133 MHz", ARCH_SH3, "Windows CE 2.11 / H/PC Pro 3.x",
                  USB_CLIENT_NONE, False, _SH3_NOTE),
    HandheldModel("sa1110-hpc2000-family", "StrongARM Handheld PC 2000 (Jornada 710/720/728)",
                  "Intel StrongARM SA-1110 206 MHz", ARCH_ARM, "Windows CE 3.0 / H/PC 2000",
                  USB_CLIENT_SA1110, True, _ARM_NOTE),
)

_HANDHELDS_BY_KEY: Dict[str, HandheldModel] = {m.key: m for m in HANDHELD_MODELS}
_MODEL_NUMBERS: Tuple[str, ...] = ("680e", "690e", "680", "690", "710", "720", "728")


def profile_for(vendor_id: Optional[int], product_id: Optional[int]) -> Optional[UsbDriverProfile]:
    """Exact vendor/product match first, then a vendor-wide wildcard, else None."""
    if vendor_id is None:
        return None
    return _BY_VID_PID.get((vendor_id, product_id)) or _BY_VID_PID.get((vendor_id, None))


def has_dock_marker(*strings: Optional[str]) -> bool:
    """True when a USB product/serial string names the retrofitted dock bridge."""
    return any(marker in (text or "").upper() for text in strings for marker in DOCK_MARKERS)


def classify(vendor_id: Optional[int], product_id: Optional[int], product: Optional[str] = None,
             serial: Optional[str] = None,
             interface_classes: Iterable[Tuple[int, int]] = ()) -> Optional[Classification]:
    """Identify a USB device by vendor/product, falling back to a CDC-ACM interface.

    ``interface_classes`` are ``(bInterfaceClass, bInterfaceSubClass)`` pairs. A serial
    bridge whose strings carry a dock marker is reported with the dock-bridge role.
    """
    profile = profile_for(vendor_id, product_id)
    if profile is None:
        if any(cls == USB_CLASS_CDC and sub == USB_SUBCLASS_ACM for cls, sub in interface_classes):
            profile = CDC_ACM_PROFILE
        else:
            return None
    role = profile.role
    if role == ROLE_SERIAL_BRIDGE and has_dock_marker(product, serial):
        role = ROLE_DOCK_BRIDGE
    return Classification(profile, role)


def handheld(key: str) -> Optional[HandheldModel]:
    return _HANDHELDS_BY_KEY.get(key)


def identify_handheld(hardware: Optional[str] = None, name: Optional[str] = None) -> Optional[HandheldModel]:
    """Best model for what the dccm handshake told us (a model number in the device name wins;
    otherwise the CPU architecture picks the family row)."""
    text = (name or "").lower()
    for number in _MODEL_NUMBERS:
        if number in text and "jornada" in text.replace(" ", ""):
            return _HANDHELDS_BY_KEY[f"jornada-{number}"]
    for number in _MODEL_NUMBERS:
        if text.endswith(number) or f"j{number}" in text:
            return _HANDHELDS_BY_KEY[f"jornada-{number}"]
    arch = (hardware or "").upper().replace("-", "").replace(" ", "")
    if "SH3" in arch or "SH7709" in arch:
        return _HANDHELDS_BY_KEY["sh3-hpc-pro-family"]
    if "ARM" in arch or "SA1110" in arch:
        return _HANDHELDS_BY_KEY["sa1110-hpc2000-family"]
    return None


def table() -> Dict[str, object]:
    """The whole table as plain data (stable ordering by key)."""
    return {
        "drivers": [asdict(p) for p in sorted(DRIVER_PROFILES, key=lambda p: p.key)],
        "handhelds": [asdict(m) for m in sorted(HANDHELD_MODELS, key=lambda m: m.key)],
        "dock_markers": list(DOCK_MARKERS),
    }


def table_json() -> str:
    """Canonical JSON of :func:`table` — what the Swift side must reproduce byte for byte."""
    return json.dumps(table(), indent=2, sort_keys=True) + "\n"
