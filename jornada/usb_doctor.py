"""The USB doctor: which USB device should carry the link, and why or why not.

Combines the I/O Registry view (:mod:`usb_registry`), the driver table
(:mod:`usb_profiles`), the optional pinned port (``~/.jornada-link/serial``)
and what is known about the handheld into one :class:`Diagnosis` — a ranked
list of serial-port candidates, the recommended port, and human findings.

The ranking is deliberately simple and deterministic: a pinned port that
exists wins; otherwise a bridge built into the dock beats a plain adapter; a
node owned by Apple's built-in driver beats a vendor extension's node (the
latter may not be openable by the user, see the ``writable`` flag); and a node
this user can actually open beats one it cannot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .usb_profiles import (ARCH_ARM, ARCH_SH3, DRIVER_BUILT_IN, ROLE_DOCK_BRIDGE, ROLE_SERIAL_BRIDGE,
                           ROLE_WINCE_USB_SYNC, Classification, HandheldModel, classify,
                           identify_handheld)
from .usb_registry import UsbDevice, usb_devices

LEVEL_OK = "ok"
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

PIN_FILE_NAME = "serial"
DOCS_PATH = "docs/usb-link.md"

# Same rule as bin/jornada-ppp and PppController.isValidSerialPath: the path
# ends up in a root pppd command line, so only plain /dev/cu.* nodes qualify.
_SERIAL_PATH_RE = re.compile(r"^/dev/cu\.[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class Finding:
    level: str
    title: str
    detail: str


@dataclass(frozen=True)
class ClassifiedDevice:
    device: UsbDevice
    classification: Optional[Classification]

    @property
    def role(self) -> Optional[str]:
        return self.classification.role if self.classification else None


@dataclass(frozen=True)
class Candidate:
    path: str
    driver: str
    writable: bool
    device: UsbDevice
    classification: Classification
    score: int


@dataclass(frozen=True)
class Diagnosis:
    devices: Tuple[ClassifiedDevice, ...]
    candidates: Tuple[Candidate, ...]
    recommended: Optional[str]
    pinned: Optional[str]
    handheld: Optional[HandheldModel]
    findings: Tuple[Finding, ...]

    @property
    def worst_level(self) -> str:
        order = (LEVEL_ERROR, LEVEL_WARN, LEVEL_INFO, LEVEL_OK)
        return next((level for level in order if any(f.level == level for f in self.findings)), LEVEL_OK)


def is_valid_serial_path(path: str) -> bool:
    return bool(_SERIAL_PATH_RE.match(path))


def default_pin_path() -> Path:
    return Path.home() / ".jornada-link" / PIN_FILE_NAME


def read_pin(path: Optional[Path] = None) -> Optional[str]:
    """The pinned serial node, or None when unset or invalid."""
    try:
        text = (path or default_pin_path()).read_text(encoding="utf-8").strip().splitlines()
    except (OSError, ValueError):
        return None
    first = text[0].strip() if text else ""
    return first if is_valid_serial_path(first) else None


def write_pin(device_path: str, path: Optional[Path] = None) -> Path:
    if not is_valid_serial_path(device_path):
        raise ValueError(f"not a plain /dev/cu.* node: {device_path!r}")
    target = path or default_pin_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(device_path + "\n", encoding="utf-8")
    return target


def clear_pin(path: Optional[Path] = None) -> bool:
    target = path or default_pin_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def classify_devices(devices: Tuple[UsbDevice, ...]) -> Tuple[ClassifiedDevice, ...]:
    return tuple(
        ClassifiedDevice(d, classify(d.vendor_id, d.product_id, d.product, d.serial, d.interface_classes))
        for d in usb_devices(devices))


def _score(candidate_role: str, driver: str, writable: bool, pinned: bool) -> int:
    score = {ROLE_DOCK_BRIDGE: 400, ROLE_SERIAL_BRIDGE: 200}.get(candidate_role, 0)
    if driver.startswith("com.apple."):
        score += 100
    score += 50 if writable else -500
    if pinned:
        score += 10_000
    return score


def rank_candidates(devices: Tuple[UsbDevice, ...], pinned: Optional[str] = None) -> Tuple[Candidate, ...]:
    """Every serial node of every recognised bridge, best first."""
    found: List[Candidate] = []
    for item in classify_devices(devices):
        if item.classification is None or item.role == ROLE_WINCE_USB_SYNC:
            continue
        for node in item.device.serial_nodes:
            found.append(Candidate(
                path=node.path, driver=node.driver, writable=node.writable, device=item.device,
                classification=item.classification,
                score=_score(item.role or "", node.driver, node.writable, pinned == node.path)))
    return tuple(sorted(found, key=lambda c: (-c.score, c.path)))


def recommend_serial_path(devices: Tuple[UsbDevice, ...], pinned: Optional[str] = None) -> Optional[str]:
    ranked = rank_candidates(devices, pinned)
    return ranked[0].path if ranked else None


def handheld_from_state(state: Optional[Dict[str, Any]]) -> Optional[HandheldModel]:
    """The handheld the dccm listener last recorded (``~/.jornada-link/connection.json``)."""
    if not state:
        return None
    device = state.get("device")
    if not isinstance(device, dict):
        return None
    hardware = device.get("hardware")
    name = device.get("name")
    return identify_handheld(hardware if isinstance(hardware, str) else None,
                             name if isinstance(name, str) else None)


def _dock_findings(handheld: Optional[HandheldModel], ranked: Tuple[Candidate, ...]) -> List[Finding]:
    if handheld is None:
        return []
    dock_bridge = next((c for c in ranked if c.classification.role == ROLE_DOCK_BRIDGE), None)
    if handheld.architecture == ARCH_SH3:
        if dock_bridge is not None:
            return [Finding(LEVEL_OK, f"{handheld.name} is linked through the dock's USB jack",
                            f"The bridge retrofitted into the dock ({dock_bridge.device.label}) carries the "
                            f"handheld's RS-232 lines to {dock_bridge.path}; the SH-3 itself never sees USB.")]
        return [Finding(LEVEL_INFO, f"{handheld.name}: the dock's USB-B jack is inert for this model",
                        "The SH7709A CPU and HD64461 companion chip have no USB device controller, so the "
                        "connector's USB pins carry nothing. Connect the dock's DB-9 (or the sync cable) to a "
                        f"USB-serial adapter, or retrofit a bridge into the dock — see {DOCS_PATH}.")]
    if handheld.architecture == ARCH_ARM:
        return [Finding(LEVEL_INFO, f"{handheld.name} can enumerate on the dock's USB jack",
                        "Its SA-1110 has a USB device controller and Windows CE 3.0 presents a USB Sync "
                        "function, but macOS ships no driver for it; jornada-link's native USB link is on the "
                        f"roadmap ({DOCS_PATH}). Use the serial path meanwhile.")]
    return []


def _sync_device_findings(items: Tuple[ClassifiedDevice, ...],
                          handheld: Optional[HandheldModel]) -> List[Finding]:
    findings: List[Finding] = []
    for item in items:
        if item.role != ROLE_WINCE_USB_SYNC:
            continue
        name = item.classification.profile.name if item.classification else item.device.label
        findings.append(Finding(
            LEVEL_INFO, f"Windows CE USB Sync device present ({item.device.vid_pid}, {name})",
            "A handheld speaking USB natively. macOS has no driver for the CE USB Sync function; "
            f"jornada-link's userspace USB link for it is on the roadmap ({DOCS_PATH})."))
        if handheld is not None and handheld.architecture == ARCH_SH3:
            findings.append(Finding(
                LEVEL_WARN, "That USB Sync device is not the SH-3 Jornada",
                f"{handheld.name} has no USB controller, so another Windows CE device is attached."))
    return findings


def _adapter_findings(items: Tuple[ClassifiedDevice, ...], ranked: Tuple[Candidate, ...],
                      recommended: Optional[str]) -> List[Finding]:
    findings: List[Finding] = []
    for item in items:
        if item.classification is None or item.role == ROLE_WINCE_USB_SYNC:
            continue
        profile = item.classification.profile
        nodes = item.device.serial_nodes
        if not nodes:
            hint = ("macOS should have created one by itself; try another port or cable."
                    if profile.macos_driver == DRIVER_BUILT_IN
                    else f"install the vendor driver ({profile.driver_name}) and reconnect the adapter.")
            findings.append(Finding(LEVEL_ERROR, f"{profile.name} has no serial node",
                                    f"{item.device.label} ({item.device.vid_pid}) is attached but no /dev/cu.* "
                                    f"node exists for it — {hint}"))
            continue
        if len(nodes) > 1:
            drivers = ", ".join(f"{n.path} ({n.driver})" for n in nodes)
            findings.append(Finding(LEVEL_INFO, f"{profile.name} exposes {len(nodes)} serial nodes",
                                    f"Two drivers are bound to the same adapter: {drivers}. "
                                    f"The link uses {recommended or nodes[0].path}."))
        for node in nodes:
            if not node.writable:
                findings.append(Finding(LEVEL_WARN, f"{node.path} is root-only",
                                        f"The node created by {node.driver} is not openable by this user. The root "
                                        "pppd can still use it, but `jornada probe` and the pre-connect flush cannot, "
                                        "so the link prefers another node of the same adapter when there is one."))
    return findings


def diagnose(devices: Tuple[UsbDevice, ...], pinned: Optional[str] = None,
             handheld: Optional[HandheldModel] = None) -> Diagnosis:
    """The complete picture for a snapshot of the USB bus (pure; no I/O)."""
    items = classify_devices(devices)
    ranked = rank_candidates(devices, pinned)
    recommended = ranked[0].path if ranked else None
    findings: List[Finding] = []

    if pinned is not None and not any(c.path == pinned for c in ranked):
        findings.append(Finding(LEVEL_WARN, f"Pinned serial port {pinned} is not present",
                                "The pin in ~/.jornada-link/serial does not match any attached adapter; "
                                "the best attached adapter is used instead. Re-pin or unpin it."))
    if not ranked and not any(i.role == ROLE_WINCE_USB_SYNC for i in items):
        findings.append(Finding(LEVEL_ERROR, "No USB-serial adapter found",
                                "Plug the adapter into the Jornada's sync cable or the dock's DB-9 port. FTDI "
                                "adapters need no driver on macOS; Prolific and WCH chips usually need the "
                                "vendor's DriverKit extension."))
    findings.extend(_sync_device_findings(items, handheld))
    findings.extend(_dock_findings(handheld, ranked))
    findings.extend(_adapter_findings(items, ranked, recommended))
    if recommended is not None:
        best = ranked[0]
        via = "pinned" if pinned == best.path else "best available"
        access = "openable by this user" if best.writable else "root-only (unpin to let the doctor choose)"
        findings.append(Finding(LEVEL_OK, f"Serial link port: {best.path} ({via})",
                                f"{best.classification.profile.name}, driver {best.driver}, {access}."))
    return Diagnosis(devices=items, candidates=ranked, recommended=recommended, pinned=pinned,
                     handheld=handheld, findings=tuple(findings))
