"""The device side of a file-based sync: one folder of documents on the Jornada."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from ..rapi import RapiClient, RapiError
from ..sendmirror import mirror_sent
from ..sync.base import Item, StoreError
from .textfiles import unique_filename

ERROR_ALREADY_EXISTS = 183


@dataclass(frozen=True)
class FileCodec:
    """How files in the folder map to a neutral record."""

    extensions: Tuple[str, ...]
    decode: Callable[[str, bytes, Optional[float]], Any]   # (file name, data, mtime) → record
    encode: Callable[[Any], Tuple[str, bytes]]             # record → (file name, data)


class DeviceFolderStore:
    """Store protocol over the files of one device directory."""

    name = "device"

    def __init__(self, client: RapiClient, folder: str, codec: FileCodec, mirror: bool = True,
                 source: str = "sync", log: Callable[[str], None] = lambda _line: None) -> None:
        self._client = client
        self._folder = folder.rstrip("\\") or "\\"
        self._codec = codec
        self._mirror = mirror
        self._source = source
        self._log = log
        self._names: set = set()

    @property
    def folder(self) -> str:
        return self._folder

    def _path(self, name: str) -> str:
        return self._folder.rstrip("\\") + "\\" + name

    def _ensure_folder(self) -> None:
        if self._client.get_file_attributes(self._folder) is not None:
            return
        parts = [p for p in self._folder.split("\\") if p]
        prefix = ""
        for part in parts:
            prefix = prefix + "\\" + part
            try:
                self._client.create_directory(prefix)
            except RapiError as exc:
                if exc.last_error != ERROR_ALREADY_EXISTS:
                    raise
        self._log(f"created {self._folder} on the device")

    def _matches(self, name: str) -> bool:
        lowered = name.lower()
        return any(lowered.endswith(ext) for ext in self._codec.extensions)

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        self._ensure_folder()
        entries = [e for e in self._client.listdir(self._folder) if not e.is_dir]
        self._names = {e.name for e in entries}
        items = []
        for entry in entries:
            if not self._matches(entry.name):
                continue
            data = self._client.download(self._path(entry.name))
            try:
                record = self._codec.decode(entry.name, data, entry.mtime)
            except (ValueError, UnicodeError) as exc:
                self._log(f"{entry.name} cannot be read ({exc}); it is left alone")
                items.append(Item(id=entry.name, record=None, problem=str(exc)))
                continue
            items.append(Item(id=entry.name, record=record, version=str(entry.mtime or "")))
        return tuple(items)

    def create(self, record: Any) -> str:
        self._ensure_folder()
        name, data = self._codec.encode(record)
        name = unique_filename(name, self._names)
        self._write(name, data)
        self._names.add(name)
        return name

    def update(self, item_id: str, record: Any) -> Optional[str]:
        if "\\" in item_id or "/" in item_id:
            raise StoreError(f"refusing to write outside {self._folder}: {item_id!r}")
        _name, data = self._codec.encode(record)
        self._write(item_id, data)
        return None

    def delete(self, item_id: str) -> None:
        if "\\" in item_id or "/" in item_id:
            raise StoreError(f"refusing to delete outside {self._folder}: {item_id!r}")
        self._client.delete_file(self._path(item_id))
        self._names.discard(item_id)

    def _write(self, name: str, data: bytes) -> None:
        path = self._path(name)
        self._client.upload(path, data)
        if self._mirror:
            try:
                mirror_sent(data, path, source=self._source)
            except (OSError, ValueError) as exc:
                self._log(f"warning: sent {path} but could not archive it locally: {exc}")
