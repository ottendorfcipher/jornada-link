"""Mac-side mirror of everything sent to the device.

The Jornada's object store is battery-backed RAM — lose power for long enough
and the device is empty. So every file pushed to the device is also archived
on the Mac, in a device-path-shaped tree, with an append-only JSONL manifest.
Prior versions are never overwritten: an existing copy with different content
is renamed aside with a timestamp before the new content is written.

The Swift app writes the same tree and manifest format (SendMirror.swift);
keep the two in sync.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .backup import _local_name

ENV_MIRROR_DIR = "JORNADA_MIRROR_DIR"
DEFAULT_MIRROR_ROOT = Path.home() / "Documents" / "Jornada Backup" / "Sent to Device"
MANIFEST_NAME = "sent-manifest.jsonl"


def mirror_root() -> Path:
    """The mirror tree root: $JORNADA_MIRROR_DIR overrides the default."""
    override = os.environ.get(ENV_MIRROR_DIR, "").strip()
    return Path(override).expanduser() if override else DEFAULT_MIRROR_ROOT


def _target_for(root: Path, device_path: str) -> Path:
    """Map a device path to a mirror file path, refusing anything that escapes."""
    parts = [_local_name(part) for part in device_path.split("\\") if part]
    if not parts:
        parts = ["_unnamed"]
    target = root.joinpath(*parts)
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise ValueError(f"device path {device_path!r} escapes the mirror root")
    return target


def _archived_name(target: Path, stamp: time.struct_time) -> Path:
    """A timestamped, collision-free sibling name for the outgoing old version."""
    base = time.strftime("%Y%m%d-%H%M%S", stamp)
    candidate = target.with_name(f"{target.stem}.{base}{target.suffix}")
    counter = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}.{base}-{counter}{target.suffix}")
        counter += 1
    return candidate


def mirror_sent(
    data: bytes,
    device_path: str,
    root: Optional[Path] = None,
    source: Optional[str] = None,
    now: Optional[float] = None,
) -> Path:
    """Archive one successfully-sent payload; returns the mirror file path.

    Identical re-sends write nothing new (the manifest still records them);
    changed content archives the previous copy under a timestamped name first.
    """
    base = root if root is not None else mirror_root()
    stamp = time.localtime(now if now is not None else time.time())
    target = _target_for(base, device_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    digest = hashlib.md5(data).hexdigest()
    unchanged = False
    if target.exists():
        if hashlib.md5(target.read_bytes()).hexdigest() == digest:
            unchanged = True
        else:
            target.rename(_archived_name(target, stamp))
    if not unchanged:
        target.write_bytes(data)

    entry: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", stamp),
        "device_path": device_path,
        "size": len(data),
        "md5": digest,
        "mirror": str(target),
        "unchanged": unchanged,
        "source": source,
    }
    with open(base / MANIFEST_NAME, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return target
