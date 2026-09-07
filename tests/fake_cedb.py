"""In-memory object-store databases for the fake device (tests/fake_device.py).

Speaks the librapi2 0.9.x wire layout for CeFindAllDatabases, CeOpenDatabase,
CeCreateDatabase, CeDeleteDatabase, CeReadRecordProps, CeWriteRecordProps,
CeDeleteRecord and CeSeekDatabase over a dict of records per database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from jornada import wire
from jornada.cedb import CEDB_PROPDELETE, PropVal, pack_record, unix_to_filetime, unpack_record
from jornada.constants import (
    CEDB_AUTOINCREMENT,
    CEDB_MAXSORTORDER,
    CEDB_SEEK_BEGINNING,
    CEDB_SEEK_CEOID,
    CEDB_SEEK_CURRENT,
    CEDB_SEEK_END,
    ERROR_INVALID_PARAMETER,
    ERROR_NO_MORE_ITEMS,
    FAD_FLAGS,
    FAD_LAST_MODIFIED,
    FAD_NAME,
    FAD_NUM_RECORDS,
    FAD_NUM_SORT_ORDER,
    FAD_OID,
    FAD_SIZE,
    FAD_SORT_SPECS,
    FAD_TYPE,
    INVALID_HANDLE_VALUE,
)

ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_HANDLE = 6
ERROR_ALREADY_EXISTS = 183
FIRST_DB_OID = 0x2000_0001
FIRST_RECORD_OID = 0x3000_0001
FIRST_DB_HANDLE = 0x8000


def reply_ok(return_value: int, extra: bytes = b"", last_error: int = 0) -> bytes:
    return wire.u32(0) + wire.u32(last_error) + wire.u32(return_value) + extra


@dataclass
class FakeDatabase:
    oid: int
    name: str
    db_type: int = 0
    records: Dict[int, Tuple[PropVal, ...]] = field(default_factory=dict)
    sort_specs: Tuple[Tuple[int, int], ...] = ()
    modified: float = 1_700_000_000.0

    @property
    def size(self) -> int:
        return sum(len(pack_record(props)) for props in self.records.values())


@dataclass
class Cursor:
    db_oid: int
    position: int = 0
    autoincrement: bool = True


class FakeDatabaseStore:
    def __init__(self) -> None:
        self.databases: Dict[int, FakeDatabase] = {}
        self.cursors: Dict[int, Cursor] = {}
        self.writes: List[Tuple[int, int, Tuple[PropVal, ...]]] = []
        self._next_db_oid = FIRST_DB_OID
        self._next_record_oid = FIRST_RECORD_OID
        self._next_handle = FIRST_DB_HANDLE

    # -- test-side helpers ----------------------------------------------------
    def create(self, name: str, records=(), db_type: int = 0) -> FakeDatabase:
        db = FakeDatabase(self._next_db_oid, name, db_type)
        self._next_db_oid += 1
        self.databases[db.oid] = db
        for props in records:
            self.add_record(db, tuple(props))
        return db

    def find(self, name: str) -> Optional[FakeDatabase]:
        wanted = name.casefold()
        return next((db for db in self.databases.values() if db.name.casefold() == wanted), None)

    def add_record(self, db: FakeDatabase, props: Tuple[PropVal, ...]) -> int:
        oid = self._next_record_oid
        self._next_record_oid += 1
        db.records[oid] = tuple(props)
        return oid

    def close(self, handle: int) -> bool:
        return self.cursors.pop(handle, None) is not None

    def _cursor_and_db(self, handle: int) -> Tuple[Optional[Cursor], Optional[FakeDatabase]]:
        cursor = self.cursors.get(handle)
        if cursor is None:
            return None, None
        return cursor, self.databases.get(cursor.db_oid)

    # -- RAPI handlers --------------------------------------------------------
    def find_all_databases(self, reader: wire.Reader) -> bytes:
        db_type = reader.u32()
        flags = reader.u16()
        matches = [db for oid, db in sorted(self.databases.items()) if db_type in (0, db.db_type)]
        out = wire.u32(0) + wire.u16(len(matches))
        for db in matches:
            out += self._find_data(db, flags)
        return out

    def _find_data(self, db: FakeDatabase, flags: int) -> bytes:
        encoded = wire.wstr(db.name)
        out = b""
        if flags & FAD_OID:
            out += wire.u32(db.oid)
        if flags & FAD_NAME:
            out += wire.u32(len(encoded) // 2)
        if flags & FAD_FLAGS:
            out += wire.u32(0)
        if flags & FAD_NAME:
            out += encoded
        if flags & FAD_TYPE:
            out += wire.u32(db.db_type)
        if flags & FAD_NUM_RECORDS:
            out += wire.u16(len(db.records))
        if flags & FAD_NUM_SORT_ORDER:
            out += wire.u16(len(db.sort_specs))
        if flags & FAD_SIZE:
            out += wire.u32(db.size)
        if flags & FAD_LAST_MODIFIED:
            ticks = unix_to_filetime(db.modified)
            out += wire.u32(ticks & 0xFFFFFFFF) + wire.u32(ticks >> 32)
        if flags & FAD_SORT_SPECS:
            specs = list(db.sort_specs) + [(0, 0)] * (CEDB_MAXSORTORDER - len(db.sort_specs))
            for cepropid, spec_flags in specs[:CEDB_MAXSORTORDER]:
                out += wire.u32(cepropid) + wire.u32(spec_flags)
        return out

    def open_database(self, reader: wire.Reader) -> bytes:
        oid = reader.u32()
        reader.u32()  # sort propid (ignored: records stay in insertion order)
        flags = reader.u32()
        if oid not in self.databases:
            return reply_ok(INVALID_HANDLE_VALUE, last_error=ERROR_FILE_NOT_FOUND)
        handle = self._next_handle
        self._next_handle += 1
        self.cursors[handle] = Cursor(oid, 0, bool(flags & CEDB_AUTOINCREMENT))
        return reply_ok(handle)

    def create_database(self, reader: wire.Reader) -> bytes:
        db_type = reader.u32()
        count = reader.u16()
        specs = tuple((reader.u32(), reader.u32()) for _ in range(count))
        present = reader.u32()
        name = reader.wchars(reader.u32()) if present == 1 else ""
        if not name:
            return reply_ok(0, last_error=ERROR_INVALID_PARAMETER)
        if self.find(name) is not None:
            return reply_ok(0, last_error=ERROR_ALREADY_EXISTS)
        db = self.create(name, db_type=db_type)
        db.sort_specs = specs
        return reply_ok(db.oid)

    def delete_database(self, reader: wire.Reader) -> bytes:
        oid = reader.u32()
        if self.databases.pop(oid, None) is None:
            return reply_ok(0, last_error=ERROR_FILE_NOT_FOUND)
        for handle in [h for h, c in self.cursors.items() if c.db_oid == oid]:
            del self.cursors[handle]
        return reply_ok(1)

    def read_record_props(self, reader: wire.Reader) -> bytes:
        handle = reader.u32()
        reader.u32()  # flags
        empty = wire.u32(0) + wire.u16(0)
        cursor, db = self._cursor_and_db(handle)
        if cursor is None or db is None:
            return reply_ok(0, empty, ERROR_INVALID_HANDLE)
        oids = list(db.records)
        if cursor.position >= len(oids):
            return reply_ok(0, empty, ERROR_NO_MORE_ITEMS)
        oid = oids[cursor.position]
        props = db.records[oid]
        if cursor.autoincrement:
            cursor.position += 1
        data = pack_record(props)
        return reply_ok(oid, wire.u32(len(data)) + wire.u16(len(props)) + data)

    def write_record_props(self, reader: wire.Reader) -> bytes:
        handle = reader.u32()
        oid = reader.u32()
        count = reader.u16()
        size = reader.u32()
        props = unpack_record(reader.take(size), count)
        cursor, db = self._cursor_and_db(handle)
        if cursor is None or db is None:
            return reply_ok(0, last_error=ERROR_INVALID_HANDLE)
        kept = tuple(p for p in props if not p.flags & CEDB_PROPDELETE)
        if oid == 0:
            oid = self.add_record(db, kept)
        elif oid in db.records:
            db.records[oid] = _merge(db.records[oid], props)
        else:
            return reply_ok(0, last_error=ERROR_INVALID_PARAMETER)
        self.writes.append((db.oid, oid, props))
        return reply_ok(oid)

    def delete_record(self, reader: wire.Reader) -> bytes:
        handle = reader.u32()
        oid = reader.u32()
        cursor, db = self._cursor_and_db(handle)
        if cursor is None or db is None:
            return reply_ok(0, last_error=ERROR_INVALID_HANDLE)
        if oid not in db.records:
            return reply_ok(0, last_error=ERROR_INVALID_PARAMETER)
        index = list(db.records).index(oid)
        del db.records[oid]
        for other in self.cursors.values():
            if other.db_oid == db.oid and other.position > index:
                other.position -= 1
        return reply_ok(1)

    def seek_database(self, reader: wire.Reader) -> bytes:
        handle = reader.u32()
        seek_type = reader.u32()
        value = reader.u32()
        cursor, db = self._cursor_and_db(handle)
        if cursor is None or db is None:
            return reply_ok(0, wire.u32(0), ERROR_INVALID_HANDLE)
        oids = list(db.records)
        signed = value - (1 << 32) if value & 0x8000_0000 else value
        if seek_type == CEDB_SEEK_BEGINNING:
            position = signed
        elif seek_type == CEDB_SEEK_END:
            position = len(oids) - 1 - signed
        elif seek_type == CEDB_SEEK_CURRENT:
            position = cursor.position + signed
        elif seek_type == CEDB_SEEK_CEOID:
            position = oids.index(value) if value in oids else -1
        else:
            return reply_ok(0, wire.u32(0), ERROR_INVALID_PARAMETER)
        if position < 0 or position >= len(oids):
            cursor.position = len(oids)
            return reply_ok(0, wire.u32(cursor.position), ERROR_NO_MORE_ITEMS)
        cursor.position = position
        return reply_ok(oids[position], wire.u32(position))


def _merge(existing: Tuple[PropVal, ...], update: Tuple[PropVal, ...]) -> Tuple[PropVal, ...]:
    """CeWriteRecordProps on an existing record: replace listed props, drop deleted ones."""
    by_id = {p.prop_id: p for p in existing}
    for prop in update:
        if prop.flags & CEDB_PROPDELETE:
            by_id.pop(prop.prop_id, None)
        else:
            by_id[prop.prop_id] = PropVal(prop.prop_id, prop.kind, prop.value)
    return tuple(by_id.values())
