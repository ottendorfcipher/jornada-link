"""Recursive delete for the device, used by ``jornada rm -r``.

RAPI only deletes one file or one empty directory at a time, so a subtree is
removed depth-first: every file first, then each directory from the leaves up.
Errors are collected per entry and the walk continues.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List

from .rapi import RapiClient, RapiError

# Refusing these prevents an accidental "erase the whole handheld".
_PROTECTED = {"", "\\"}


@dataclass
class RemoveStats:
    files_deleted: int = 0
    directories_deleted: int = 0
    errors: List[str] = field(default_factory=list)


def remove_tree(client: RapiClient, device_path: str,
                log: Callable[[str], None] = print) -> RemoveStats:
    """Delete ``device_path`` and everything under it (depth-first)."""
    core = device_path.strip().strip("\\").strip()
    normalized = "\\" + core if core else "\\"
    if normalized in _PROTECTED:
        raise ValueError("refusing to recursively delete the device root")

    stats = RemoveStats()
    attributes = client.get_file_attributes(normalized)
    if attributes is None:
        raise RapiError(f"{normalized} does not exist")
    if not _is_directory(attributes):
        _delete_file(client, normalized, stats, log)
        return stats

    _remove_directory_recursive(client, normalized, stats, log)
    return stats


def _is_directory(attributes: int) -> bool:
    from .constants import FILE_ATTRIBUTE_DIRECTORY
    return bool(attributes & FILE_ATTRIBUTE_DIRECTORY)


def _delete_file(client: RapiClient, path: str, stats: RemoveStats,
                 log: Callable[[str], None]) -> None:
    try:
        client.delete_file(path)
        stats.files_deleted += 1
        log(f"  deleted {path}")
    except (RapiError, OSError) as exc:
        stats.errors.append(f"delete {path}: {exc}")
        log(f"  !! {path}: {exc}")


def _remove_directory_recursive(client: RapiClient, path: str, stats: RemoveStats,
                                log: Callable[[str], None]) -> None:
    try:
        entries = client.listdir(path)
    except (RapiError, OSError) as exc:
        stats.errors.append(f"list {path}: {exc}")
        log(f"  !! cannot list {path}: {exc}")
        return
    for entry in entries:
        child = path.rstrip("\\") + "\\" + entry.name
        if entry.is_dir:
            _remove_directory_recursive(client, child, stats, log)
        else:
            _delete_file(client, child, stats, log)
    try:
        client.remove_directory(path)
        stats.directories_deleted += 1
        log(f"  removed {path}")
    except (RapiError, OSError) as exc:
        stats.errors.append(f"rmdir {path}: {exc}")
        log(f"  !! {path}: {exc}")
