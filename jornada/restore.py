"""Restore a backup or sent-mirror tree onto the device.

The inverse of ``jornada.backup``: walks a device-path-shaped local tree and
pushes it back over RAPI, recreating directories and skipping what is already
there (by name + size — the old protocol offers no remote checksums). Manifest
files, ``.DS_Store``, and the sent-mirror's timestamped archive versions are
never restored.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .rapi import FileEntry, RapiClient, RapiError
from .sendmirror import MANIFEST_NAME

BACKUP_MANIFEST_NAME = "backup-manifest.json"
ERROR_ALREADY_EXISTS = 183

# sent-mirror archive versions: "<stem>.YYYYMMDD-HHMMSS[-N][.ext]"
_ARCHIVED_VERSION = re.compile(r"\.\d{8}-\d{6}(-\d+)?(\.[^.]*)?$")
_EXCLUDED_NAMES = {MANIFEST_NAME, BACKUP_MANIFEST_NAME, ".DS_Store"}


@dataclass
class RestoreStats:
    files_sent: int = 0
    bytes_sent: int = 0
    directories_created: int = 0
    skipped_existing: int = 0
    skipped_archived: int = 0
    errors: List[str] = field(default_factory=list)


def is_archived_version(name: str) -> bool:
    """True for the timestamped prior-version copies the sent mirror keeps."""
    return bool(_ARCHIVED_VERSION.search(name))


def _should_skip(name: str) -> bool:
    return name in _EXCLUDED_NAMES


def plan_size(local_root: Path) -> int:
    """Upper bound of bytes a restore could send (before existing-file skips)."""
    total = 0
    for path in local_root.rglob("*"):
        if path.is_file() and not _should_skip(path.name) and not is_archived_version(path.name):
            total += path.stat().st_size
    return total


def _ensure_directory(client: RapiClient, device_path: str, stats: RestoreStats,
                      dry_run: bool, log: Callable[[str], None]) -> None:
    if dry_run:
        log(f"  would create {device_path}")
        return
    try:
        client.create_directory(device_path)
        stats.directories_created += 1
        log(f"  created {device_path}")
    except RapiError as exc:
        if exc.last_error != ERROR_ALREADY_EXISTS:
            raise


def ensure_device_path(client: RapiClient, device_path: str, stats: RestoreStats,
                       dry_run: bool, log: Callable[[str], None]) -> None:
    """Create every component of ``device_path`` that does not exist yet."""
    parts = [part for part in device_path.split("\\") if part]
    prefix = ""
    for part in parts:
        prefix = prefix + "\\" + part
        _ensure_directory(client, prefix, stats, dry_run, log)


def restore_tree(
    client: RapiClient,
    local_root: Path,
    device_root: str = "\\",
    force: bool = False,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> RestoreStats:
    """Push ``local_root`` onto the device under ``device_root``."""
    stats = RestoreStats()
    started = time.time()
    if device_root not in ("", "\\"):
        ensure_device_path(client, device_root, stats, dry_run, log)

    def listing_for(device_dir: str) -> Dict[str, FileEntry]:
        try:
            return {entry.name.lower(): entry for entry in client.listdir(device_dir)}
        except (RapiError, OSError) as exc:
            stats.errors.append(f"list {device_dir}: {exc}")
            log(f"  !! cannot list {device_dir}: {exc}")
            return {}

    def walk(local_dir: Path, device_dir: str) -> None:
        existing = listing_for(device_dir)
        for child in sorted(local_dir.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            name = child.name
            if _should_skip(name):
                continue
            if child.is_file() and is_archived_version(name):
                stats.skipped_archived += 1
                continue
            device_path = device_dir.rstrip("\\") + "\\" + name
            if child.is_dir():
                if name.lower() not in existing:
                    try:
                        _ensure_directory(client, device_path, stats, dry_run, log)
                    except (RapiError, OSError) as exc:
                        stats.errors.append(f"mkdir {device_path}: {exc}")
                        log(f"  !! mkdir {device_path}: {exc}")
                        continue
                walk(child, device_path)
                continue
            data = child.read_bytes()
            remote = existing.get(name.lower())
            if remote is not None and not remote.is_dir \
                    and remote.size == len(data) and not force:
                stats.skipped_existing += 1
                continue
            if dry_run:
                log(f"  would send {device_path} ({len(data):,} B)")
                stats.files_sent += 1
                stats.bytes_sent += len(data)
                continue
            try:
                client.upload(device_path, data)
                stats.files_sent += 1
                stats.bytes_sent += len(data)
                log(f"  {device_path} ({len(data):,} B)")
            except (RapiError, OSError) as exc:
                stats.errors.append(f"send {device_path}: {exc}")
                log(f"  !! {device_path}: {exc}")

    walk(local_root, device_root if device_root else "\\")
    verb = "planned" if dry_run else "restored"
    log(f"{verb} {stats.files_sent} file(s), {stats.bytes_sent:,} bytes in "
        f"{time.time() - started:.1f}s — {stats.skipped_existing} already on device, "
        f"{stats.skipped_archived} archived version(s) left out, {len(stats.errors)} error(s)")
    return stats


def free_space_warning(client: RapiClient, planned_bytes: int) -> Optional[str]:
    """A human warning when the plan exceeds the device's free object store."""
    try:
        store = client.get_store_information()
    except (RapiError, OSError):
        return None
    if planned_bytes > store.free_size:
        return (f"planned restore is {planned_bytes:,} bytes but the device reports "
                f"only {store.free_size:,} free — existing-file skips may save you, "
                f"or the restore may fail partway")
    return None
