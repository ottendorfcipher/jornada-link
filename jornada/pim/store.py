"""The device side of a sync: one Pocket Outlook database read and written over RAPI.

Every write is preceded, once per session, by a JSON snapshot of the whole
database under the Jornada Backup folder — the object store is battery-backed
RAM and a sync must never be the only copy of a record.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from ..cedb import PropVal, Record
from ..rapi import RapiClient, RapiError
from ..sendmirror import mirror_root
from ..sync.base import Item, StoreError

DEFAULT_SNAPSHOT_DIR = mirror_root().parent / "PIM Snapshots"


@dataclass(frozen=True)
class Codec:
    """How a database's records map to a neutral model."""

    database: str
    decode: Callable[[Record], Any]
    encode: Callable[[Any, Optional[Record]], Tuple[PropVal, ...]]
    read_only: Callable[[Any], bool] = lambda _model: False
    create_if_missing: bool = False
    db_type: int = 0


class DeviceStore:
    """Store protocol over a device database (see :mod:`jornada.sync.base`)."""

    name = "device"

    def __init__(self, client: RapiClient, codec: Codec, snapshot_dir: Optional[Path] = None,
                 log: Callable[[str], None] = lambda _line: None) -> None:
        self._client = client
        self._codec = codec
        self._snapshot_dir = snapshot_dir if snapshot_dir is not None else DEFAULT_SNAPSHOT_DIR
        self._log = log
        self._raw: Dict[int, Record] = {}
        self._snapshot_taken: Optional[Path] = None

    @property
    def database(self) -> str:
        return self._codec.database

    # -- database handle ------------------------------------------------------
    def _open(self) -> int:
        info = self._client.find_database(self._codec.database)
        if info is None:
            if not self._codec.create_if_missing:
                raise StoreError(f"the device has no database called {self._codec.database!r}")
            oid = self._client.create_database(self._codec.database, self._codec.db_type)
            self._log(f"created database {self._codec.database!r} on the device")
            return self._client.open_database(oid)
        return self._client.open_database(info.oid)

    def _records(self) -> Tuple[Record, ...]:
        handle = self._open()
        try:
            return tuple(self._client.iter_records(handle))
        finally:
            self._client.close_handle(handle)

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        records = self._records()
        self._raw = {record.oid: record for record in records}
        items = []
        for record in records:
            try:
                model = self._codec.decode(record)
            except (ValueError, TypeError, OverflowError) as exc:
                self._log(f"skipping record 0x{record.oid:08x}: {exc}")
                continue
            items.append(Item(id=str(record.oid), record=model))
        return tuple(items)

    def create(self, record: Any) -> str:
        self._ensure_snapshot()
        handle = self._open()
        try:
            oid = self._client.write_record(handle, self._codec.encode(record, None))
        finally:
            self._client.close_handle(handle)
        self._raw[oid] = Record(oid, self._codec.encode(record, None))
        return str(oid)

    def update(self, item_id: str, record: Any) -> Optional[str]:
        oid = int(item_id)
        existing = self._raw.get(oid)
        if existing is not None and self._codec.read_only(self._codec.decode(existing)):
            raise StoreError(f"record 0x{oid:08x} is recurring; the device copy is left unchanged")
        self._ensure_snapshot()
        handle = self._open()
        try:
            self._client.write_record(handle, self._codec.encode(record, existing), oid=oid)
        finally:
            self._client.close_handle(handle)
        return None

    def delete(self, item_id: str) -> None:
        oid = int(item_id)
        self._ensure_snapshot()
        handle = self._open()
        try:
            self._client.delete_record(handle, oid)
        finally:
            self._client.close_handle(handle)
        self._raw.pop(oid, None)

    # -- safety net -----------------------------------------------------------
    def _ensure_snapshot(self) -> None:
        if self._snapshot_taken is None:
            self._snapshot_taken = self.snapshot()

    def snapshot(self, directory: Optional[Path] = None, now: Optional[float] = None) -> Path:
        """Write every raw record of the database as JSON; returns the file path."""
        target_dir = directory if directory is not None else self._snapshot_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        records = self._records() if not self._raw else tuple(self._raw.values())
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
        safe = "".join(c if c.isalnum() else "_" for c in self._codec.database)
        target = target_dir / f"{safe}.{stamp}.json"
        payload = {"database": self._codec.database, "created": stamp,
                   "records": [r.to_json() for r in records]}
        tmp = target.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, ensure_ascii=False)
        os.replace(tmp, target)
        self._log(f"snapshot of {self._codec.database!r} ({len(records)} records) → {target}")
        return target


def restore_snapshot(client: RapiClient, path: Path, log: Callable[[str], None] = print) -> int:
    """Push every record of a snapshot file back as new records. Returns the count."""
    from ..cedb import KIND_NAMES
    kinds = {name: kind for kind, name in KIND_NAMES.items()}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    name = payload["database"]
    info = client.find_database(name)
    oid = info.oid if info is not None else client.create_database(name)
    handle_id = client.open_database(oid)
    count = 0
    try:
        for entry in payload["records"]:
            props = tuple(_prop_from_json(p, kinds) for p in entry["props"])
            client.write_record(handle_id, props)
            count += 1
    finally:
        client.close_handle(handle_id)
    log(f"restored {count} record(s) into {name!r}")
    return count


def _prop_from_json(data: Dict[str, Any], kinds: Dict[str, int]) -> PropVal:
    kind = kinds.get(data["kind"])
    if kind is None:
        raise StoreError(f"cannot restore a property of type {data['kind']!r}")
    value = bytes.fromhex(data["value"]) if data["kind"] == "blob" else data["value"]
    return PropVal(int(data["id"]), kind, value, int(data.get("flags", 0)))
