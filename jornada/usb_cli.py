"""``jornada usb``: the status of the USB/serial link, auto-detected.

``jornada usb`` (the default) prints which adapter is attached, which serial
port the link will open, and anything the doctor thinks is wrong. ``pick``
prints just the chosen port for scripts (``bin/jornada-ppp`` calls it);
``pin``/``unpin`` are the manual override of that choice via the
``~/.jornada-link/serial`` pin file. The handheld is recognised from the last
dccm session; there is nothing to configure.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import asdict
from typing import Any, Dict, List

from .state import DEFAULT_STATE_PATH, read_state
from .usb_doctor import (LEVEL_ERROR, LEVEL_WARN, Diagnosis, clear_pin, default_pin_path,
                         diagnose, handheld_from_state, is_valid_serial_path, read_pin,
                         write_pin)
from .usb_registry import RegistryError, read_registry

ACTIONS = ("status", "pick", "pin", "unpin")


def _diagnosis() -> Diagnosis:
    try:
        devices = read_registry()
    except RegistryError as exc:
        raise SystemExit(f"cannot read the USB registry: {exc}") from exc
    handheld = handheld_from_state(read_state(DEFAULT_STATE_PATH))
    return diagnose(devices, pinned=read_pin(), handheld=handheld)


def diagnosis_as_dict(diag: Diagnosis) -> Dict[str, Any]:
    """A JSON-friendly view of the status."""
    adapter = next((item for item in diag.devices if item.classification is not None), None)
    return {
        "adapter": None if adapter is None else {
            "vid_pid": adapter.device.vid_pid,
            "label": adapter.device.label,
            "profile": adapter.classification.profile.key,
            "serial": adapter.device.serial,
        },
        "recommended": diag.recommended,
        "pinned": diag.pinned,
        "handheld": diag.handheld.key if diag.handheld else None,
        "findings": [asdict(f) for f in diag.findings if f.level in (LEVEL_WARN, LEVEL_ERROR)],
    }


def format_status(diag: Diagnosis) -> List[str]:
    """The condensed status: adapter, port, handheld, and only real problems."""
    adapter = next((item for item in diag.devices if item.classification is not None), None)
    lines = []
    if adapter is None:
        lines.append("adapter : none — no USB-serial adapter attached")
    else:
        lines.append(f"adapter : {adapter.device.label} ({adapter.classification.profile.chip})")
    if diag.recommended is None:
        lines.append("port    : none available")
    else:
        candidate = next((c for c in diag.candidates if c.path == diag.recommended), None)
        tags = []
        if diag.pinned == diag.recommended:
            tags.append("pinned")
        if candidate is not None and not candidate.writable:
            tags.append("root-only")
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        driver = f"  driver {candidate.driver}" if candidate is not None else ""
        lines.append(f"port    : {diag.recommended}{driver}{suffix}")
    if diag.handheld:
        lines.append(f"handheld: {diag.handheld.name}")
    for finding in diag.findings:
        if finding.level == LEVEL_ERROR:
            lines.append(f"ERR   {finding.title} — {finding.detail}")
        elif finding.level == LEVEL_WARN:
            lines.append(f"WARN  {finding.title} — {finding.detail}")
    return lines


def _node_exists(path: str) -> bool:
    try:
        return stat.S_ISCHR(os.stat(path).st_mode)
    except OSError:
        return False


def _cmd_pin(args: argparse.Namespace) -> int:
    path = args.path
    if not path:
        raise SystemExit("usage: jornada usb pin /dev/cu.usbserial-XXXX")
    if not is_valid_serial_path(path):
        raise SystemExit(f"refusing {path!r}: only plain /dev/cu.* nodes can be pinned")
    if not _node_exists(path):
        raise SystemExit(f"{path} is not an attached serial device (see `jornada usb`)")
    target = write_pin(path)
    print(f"pinned {path} in {target} — Connect and bin/jornada-ppp will use it")
    return 0


def _cmd_unpin() -> int:
    if clear_pin():
        print(f"removed {default_pin_path()} — the best attached adapter is used again")
    else:
        print("no serial port was pinned")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.action == "pin":
        return _cmd_pin(args)
    if args.action == "unpin":
        return _cmd_unpin()
    diag = _diagnosis()
    if args.action == "pick":
        if diag.recommended is None:
            sys.stderr.write("no USB-serial adapter found\n")
            return 1
        print(diag.recommended)
        return 0
    if args.json:
        print(json.dumps(diagnosis_as_dict(diag), indent=2, sort_keys=True))
    else:
        print("\n".join(format_status(diag)))
    return 1 if diag.worst_level == LEVEL_ERROR else 0


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser("usb", help="USB/Serial link status: which adapter and port carry the link (auto-detected)")
    p.add_argument("action", nargs="?", default="status", choices=ACTIONS,
                   help="status (default), pick (port only, for scripts), pin PATH, unpin")
    p.add_argument("path", nargs="?", help="serial node for `pin`, e.g. /dev/cu.usbserial-XXXX")
    p.add_argument("--json", action="store_true", help="machine-readable status")
    p.set_defaults(func=run)
