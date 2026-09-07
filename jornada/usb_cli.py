"""``jornada usb``: the USB doctor from the command line.

Actions: ``doctor`` (default) explains what is attached and which port the link
will use; ``list`` is the bare device table; ``pick`` prints the recommended
port for scripts (``bin/jornada-ppp`` calls it); ``pin``/``unpin`` manage the
``~/.jornada-link/serial`` pin file; ``profiles`` dumps the driver table (the
Swift parity check compares its own dump against this).
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from . import usb_profiles
from .state import DEFAULT_STATE_PATH, read_state
from .usb_doctor import (LEVEL_ERROR, LEVEL_INFO, LEVEL_OK, LEVEL_WARN, Diagnosis, clear_pin,
                         default_pin_path, diagnose, handheld_from_state, is_valid_serial_path,
                         read_pin, write_pin)
from .usb_registry import RegistryError, read_registry

ACTIONS = ("doctor", "list", "pick", "pin", "unpin", "profiles")
_LEVEL_MARK = {LEVEL_OK: "OK  ", LEVEL_INFO: "info", LEVEL_WARN: "WARN", LEVEL_ERROR: "ERR "}


def _handheld(args: argparse.Namespace) -> Optional[usb_profiles.HandheldModel]:
    if args.model:
        model = usb_profiles.handheld(args.model)
        if model is None:
            known = ", ".join(m.key for m in usb_profiles.HANDHELD_MODELS)
            raise SystemExit(f"unknown handheld model {args.model!r}; choose from: {known}")
        return model
    return handheld_from_state(read_state(DEFAULT_STATE_PATH))


def _diagnosis(args: argparse.Namespace) -> Diagnosis:
    try:
        devices = read_registry()
    except RegistryError as exc:
        raise SystemExit(f"cannot read the USB registry: {exc}") from exc
    return diagnose(devices, pinned=read_pin(), handheld=_handheld(args))


def diagnosis_as_dict(diag: Diagnosis) -> Dict[str, Any]:
    """A JSON-friendly view of a :class:`Diagnosis`."""
    return {
        "devices": [{
            "vid_pid": item.device.vid_pid,
            "label": item.device.label,
            "vendor": item.device.vendor,
            "product": item.device.product,
            "serial": item.device.serial,
            "location_id": item.device.location_id,
            "profile": item.classification.profile.key if item.classification else None,
            "role": item.role,
            "serial_nodes": [asdict(n) for n in item.device.serial_nodes],
        } for item in diag.devices],
        "candidates": [{
            "path": c.path, "driver": c.driver, "writable": c.writable, "score": c.score,
            "profile": c.classification.profile.key, "role": c.classification.role,
        } for c in diag.candidates],
        "recommended": diag.recommended,
        "pinned": diag.pinned,
        "handheld": diag.handheld.key if diag.handheld else None,
        "findings": [asdict(f) for f in diag.findings],
    }


def format_list(diag: Diagnosis) -> List[str]:
    if not diag.devices:
        return ["no USB devices attached (hubs are not listed)"]
    lines = []
    for item in diag.devices:
        what = item.classification.profile.name if item.classification else "not a link device"
        role = f" [{item.role}]" if item.role else ""
        lines.append(f"{item.device.vid_pid}  {item.device.label}  — {what}{role}")
        for node in item.device.serial_nodes:
            marks = []
            if node.path == diag.recommended:
                marks.append("selected")
            if node.path == diag.pinned:
                marks.append("pinned")
            if not node.writable:
                marks.append("not openable")
            suffix = f"  ({', '.join(marks)})" if marks else ""
            lines.append(f"    {node.path}  driver {node.driver}{suffix}")
    return lines


def format_doctor(diag: Diagnosis) -> List[str]:
    lines = []
    if diag.handheld:
        lines.append(f"handheld: {diag.handheld.name} — {diag.handheld.cpu}; "
                     f"dock USB jack {'live' if diag.handheld.dock_usb else 'inert'} for this model")
    else:
        lines.append("handheld: unknown (connect PC Link once, or pass --model, e.g. --model jornada-680e)")
    lines.extend(format_list(diag))
    lines.append("")
    for finding in diag.findings:
        lines.append(f"{_LEVEL_MARK[finding.level]}  {finding.title}")
        lines.append(f"      {finding.detail}")
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
        raise SystemExit(f"{path} is not an attached serial device (see `jornada usb list`)")
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
    action = args.action
    if action == "profiles":
        sys.stdout.write(usb_profiles.table_json())
        return 0
    if action == "pin":
        return _cmd_pin(args)
    if action == "unpin":
        return _cmd_unpin()
    diag = _diagnosis(args)
    if action == "pick":
        if diag.recommended is None:
            sys.stderr.write("no USB-serial adapter found\n")
            return 1
        print(diag.recommended)
        return 0
    if args.json:
        print(json.dumps(diagnosis_as_dict(diag), indent=2, sort_keys=True))
        return 0
    lines = format_list(diag) if action == "list" else format_doctor(diag)
    print("\n".join(lines))
    return 0 if diag.worst_level != LEVEL_ERROR or action == "list" else 1


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser("usb", help="USB doctor: which adapter carries the link, and what the dock's USB jack can do")
    p.add_argument("action", nargs="?", default="doctor", choices=ACTIONS,
                   help="doctor (default), list, pick, pin PATH, unpin, profiles")
    p.add_argument("path", nargs="?", help="serial node for `pin`, e.g. /dev/cu.usbserial-XXXX")
    p.add_argument("--model", help="handheld model key, e.g. jornada-680e (default: last dccm session)")
    p.add_argument("--json", action="store_true", help="machine-readable output for doctor/list")
    p.set_defaults(func=run)
