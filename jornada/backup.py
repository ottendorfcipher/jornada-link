"""Recursive device backup: mirror a device subtree into a local directory."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .constants import FILE_ATTRIBUTE_INROM, FILE_ATTRIBUTE_ROMMODULE
from .rapi import FileEntry, RapiClient

ROM_MASK = FILE_ATTRIBUTE_INROM | FILE_ATTRIBUTE_ROMMODULE


@dataclass
class BackupStats:
    files: int = 0
    skipped_rom: int = 0
    skipped_existing: int = 0
    bytes_copied: int = 0
    errors: List[str] = field(default_factory=list)


# Windows-CE path separators and traversal tokens are never valid as a single
# local filename component. A hostile or buggy RAPI server controls these
# names, so we neutralize them rather than trust the device (OWASP A01/A05).
def _local_name(name: str) -> str:
    """Reduce a device entry name to one safe local path component."""
    cleaned = name.replace("/", "_").replace("\\", "_").replace(":", "_").replace("\x00", "")
    cleaned = cleaned.strip()
    if cleaned in ("", ".", "..") or cleaned.strip(".") == "":
        cleaned = "_" + (cleaned or "unnamed")
    return cleaned


def _safe_child(parent: Path, name: str) -> Path:
    """Join ``name`` under ``parent`` and refuse anything that escapes it."""
    child = (parent / _local_name(name))
    resolved_parent = parent.resolve()
    if resolved_parent not in child.resolve().parents and child.resolve() != resolved_parent:
        raise ValueError(f"unsafe device entry name {name!r} escapes {parent}")
    return child


def backup_tree(
    client: RapiClient,
    device_root: str,
    local_root: Path,
    include_rom: bool = False,
    include_cards: bool = False,
    log: Callable[[str], None] = print,
) -> BackupStats:
    """Copy ``device_root`` (device syntax, e.g. "\\") into ``local_root``.

    Storage cards are replaceable removable media; they are skipped unless
    ``include_cards`` is set.
    """
    stats = BackupStats()
    manifest: List[dict] = []
    started = time.time()

    def walk(device_dir: str, local_dir: Path) -> None:
        try:
            entries = client.listdir(device_dir)
        except Exception as exc:  # keep going: one unreadable dir shouldn't kill a backup
            stats.errors.append(f"list {device_dir}: {exc}")
            log(f"  !! cannot list {device_dir}: {exc}")
            return
        local_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            device_path = device_dir.rstrip("\\") + "\\" + entry.name
            if entry.is_dir:
                if not include_cards and entry.name.lower().startswith("storage card"):
                    log(f"  (skipping removable {device_path}; use --include-cards)")
                    continue
                walk(device_path, _safe_child(local_dir, entry.name))
                continue
            if not include_rom and (entry.attributes & ROM_MASK):
                stats.skipped_rom += 1
                continue
            target = _safe_child(local_dir, entry.name)
            if target.exists() and target.stat().st_size == entry.size:
                stats.skipped_existing += 1
                manifest.append(_manifest_row(device_path, entry, None))
                continue
            try:
                digest = hashlib.md5()
                with open(target, "wb") as sink:
                    for chunk in client.iter_download(device_path):
                        sink.write(chunk)
                        digest.update(chunk)
                stats.files += 1
                stats.bytes_copied += entry.size
                if entry.mtime:
                    try:
                        import os
                        os.utime(target, (entry.mtime, entry.mtime))
                    except OSError:
                        pass
                manifest.append(_manifest_row(device_path, entry, digest.hexdigest()))
                log(f"  {device_path} ({entry.size:,} B)")
            except Exception as exc:
                stats.errors.append(f"get {device_path}: {exc}")
                log(f"  !! {device_path}: {exc}")

    walk(device_root, local_root)
    manifest_path = local_root / "backup-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump({
            "device_root": device_root,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_s": round(time.time() - started, 1),
            "entries": manifest,
        }, handle, indent=1)
    return stats


def _manifest_row(device_path: str, entry: FileEntry, md5: Optional[str]) -> dict:
    return {
        "path": device_path,
        "size": entry.size,
        "mtime": entry.mtime,
        "attributes": entry.attributes,
        "md5": md5,
    }
