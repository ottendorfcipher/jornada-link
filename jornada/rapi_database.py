"""Object-store database calls for the RAPI client (librapi2 0.9.x ``database.c``).

Mixed into :class:`jornada.rapi.RapiClient`. The wire layout of every call is
the one librapi2 verified against CE 2.x devices: ``CeOpenDatabase`` takes an
object identifier (not a name — find it with :meth:`find_database` first),
``CeReadRecordProps`` returns ``last_error, oid, size, count, buffer`` and
``CeWriteRecordProps`` sends ``handle, oid, count, size, buffer`` where the
buffer is :func:`jornada.cedb.pack_record`'s output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

from . import wire
from .cedb import PropVal, Record, filetime_to_unix, pack_record, unpack_record
from .constants import (
    CEDB_ALLOWREALLOC,
    CEDB_AUTOINCREMENT,
    CEDB_MAXSORTORDER,
    CMD_CREATE_DATABASE,
    CMD_DELETE_DATABASE,
    CMD_DELETE_RECORD,
    CMD_FIND_ALL_DATABASES,
    CMD_OPEN_DATABASE,
    CMD_READ_RECORD_PROPS,
    CMD_SEEK_DATABASE,
    CMD_WRITE_RECORD_PROPS,
    ERROR_NO_MORE_ITEMS,
    FAD_FLAGS,
    FAD_LAST_MODIFIED,
    FAD_LISTING,
    FAD_NAME,
    FAD_NUM_RECORDS,
    FAD_NUM_SORT_ORDER,
    FAD_OID,
    FAD_SIZE,
    FAD_SORT_SPECS,
    FAD_TYPE,
    INVALID_HANDLE_VALUE,
)
from .rapi_errors import RapiError


@dataclass(frozen=True)
class DatabaseInfo:
    """One entry of a CeFindAllDatabases listing."""

    oid: int
    name: str
    db_type: int = 0
    flags: int = 0
    num_records: int = 0
    num_sort_order: int = 0
    size: int = 0
    last_modified: Optional[float] = None
    sort_specs: Tuple[Tuple[int, int], ...] = ()


class DatabaseCalls:
    """CeXxxDatabase / CeXxxRecord wrappers; expects ``call`` and ``_call_simple``."""

    # -- enumeration ----------------------------------------------------------
    def find_all_databases(self, db_type: int = 0, flags: int = FAD_LISTING) -> List[DatabaseInfo]:
        """CeFindAllDatabases: every database of ``db_type`` (0 = all)."""
        reader = self.call(CMD_FIND_ALL_DATABASES, wire.u32(db_type) + wire.u16(flags))
        count = reader.u16()
        return [_read_find_data(reader, flags) for _ in range(count)]

    def find_database(self, name: str) -> Optional[DatabaseInfo]:
        """The database called ``name`` (case-insensitive), or None."""
        wanted = name.casefold()
        return next((db for db in self.find_all_databases() if db.name.casefold() == wanted), None)

    # -- lifecycle ------------------------------------------------------------
    def open_database(self, oid: int, flags: int = CEDB_AUTOINCREMENT, sort_propid: int = 0) -> int:
        """CeOpenDatabase by object identifier; returns a handle for CeCloseHandle."""
        payload = wire.u32(oid) + wire.u32(sort_propid) + wire.u32(flags)
        reply = self._call_simple(CMD_OPEN_DATABASE, payload)
        if reply.return_value == INVALID_HANDLE_VALUE or reply.return_value == 0:
            raise RapiError(f"cannot open database 0x{oid:08x} (Win32 error {reply.last_error})",
                            reply.last_error)
        return reply.return_value

    def create_database(self, name: str, db_type: int = 0,
                        sort_specs: Sequence[Tuple[int, int]] = ()) -> int:
        """CeCreateDatabase; ``sort_specs`` are (CEPROPID, flags) pairs. Returns the OID."""
        if len(sort_specs) > CEDB_MAXSORTORDER:
            raise ValueError(f"at most {CEDB_MAXSORTORDER} sort orders are allowed")
        payload = wire.u32(db_type) + wire.u16(len(sort_specs))
        for cepropid, spec_flags in sort_specs:
            payload += wire.u32(cepropid) + wire.u32(spec_flags)
        payload += wire.string(name)
        reply = self._call_simple(CMD_CREATE_DATABASE, payload)
        if reply.return_value == 0:
            raise RapiError(f"cannot create database {name!r} (Win32 error {reply.last_error})",
                            reply.last_error)
        return reply.return_value

    def delete_database(self, oid: int) -> None:
        self._check_bool("CeDeleteDatabase", self._call_simple(CMD_DELETE_DATABASE, wire.u32(oid)))

    # -- records --------------------------------------------------------------
    def read_record(self, handle: int, flags: int = CEDB_ALLOWREALLOC) -> Optional[Record]:
        """CeReadRecordProps at the cursor (advances it when opened with autoincrement).

        Returns None once the cursor is past the last record.
        """
        payload = wire.u32(handle) + wire.u32(flags) + wire.u32(0) * 3 + wire.u16(0)
        reply = self._call_simple(CMD_READ_RECORD_PROPS, payload)
        size = reply.reader.u32()
        count = reply.reader.u16()
        data = reply.reader.take(size) if size else b""
        if reply.return_value == 0:
            if reply.last_error == ERROR_NO_MORE_ITEMS:
                return None
            raise RapiError(f"CeReadRecordProps failed (Win32 error {reply.last_error})", reply.last_error)
        return Record(reply.return_value, unpack_record(data, count))

    def iter_records(self, handle: int) -> Iterator[Record]:
        while True:
            record = self.read_record(handle)
            if record is None:
                return
            yield record

    def write_record(self, handle: int, props: Sequence[PropVal], oid: int = 0) -> int:
        """CeWriteRecordProps: ``oid`` 0 creates a record; otherwise updates it. Returns the OID."""
        data = pack_record(tuple(props))
        payload = wire.u32(handle) + wire.u32(oid) + wire.u16(len(props)) + wire.u32(len(data)) + data
        reply = self._call_simple(CMD_WRITE_RECORD_PROPS, payload)
        if reply.return_value == 0:
            raise RapiError(f"CeWriteRecordProps failed (Win32 error {reply.last_error})", reply.last_error)
        return reply.return_value

    def delete_record(self, handle: int, oid: int) -> None:
        payload = wire.u32(handle) + wire.u32(oid)
        self._check_bool("CeDeleteRecord", self._call_simple(CMD_DELETE_RECORD, payload))

    def seek_database(self, handle: int, seek_type: int, value: int = 0) -> Tuple[int, int]:
        """CeSeekDatabase (CEDB_SEEK_* types); returns ``(oid, index)`` — oid 0 when nothing is there."""
        payload = wire.u32(handle) + wire.u32(seek_type) + wire.u32(value & 0xFFFFFFFF)
        reply = self._call_simple(CMD_SEEK_DATABASE, payload)
        index = reply.reader.u32()
        return reply.return_value, index

    # -- convenience ----------------------------------------------------------
    def read_all_records(self, name: str) -> Tuple[DatabaseInfo, List[Record]]:
        """Open the database called ``name`` and return every record."""
        info = self.find_database(name)
        if info is None:
            raise RapiError(f"no database called {name!r} on the device")
        handle = self.open_database(info.oid)
        try:
            return info, list(self.iter_records(handle))
        finally:
            self.close_handle(handle)


def _read_find_data(reader: wire.Reader, flags: int) -> DatabaseInfo:
    oid = reader.u32() if flags & FAD_OID else 0
    name_size = reader.u32() if flags & FAD_NAME else 0
    db_flags = reader.u32() if flags & FAD_FLAGS else 0
    name = reader.wchars(name_size) if flags & FAD_NAME else ""
    db_type = reader.u32() if flags & FAD_TYPE else 0
    num_records = reader.u16() if flags & FAD_NUM_RECORDS else 0
    num_sort_order = reader.u16() if flags & FAD_NUM_SORT_ORDER else 0
    size = reader.u32() if flags & FAD_SIZE else 0
    modified = None
    if flags & FAD_LAST_MODIFIED:
        low, high = reader.u32(), reader.u32()
        modified = filetime_to_unix((high << 32) | low)
    specs: Tuple[Tuple[int, int], ...] = ()
    if flags & FAD_SORT_SPECS:
        specs = tuple((reader.u32(), reader.u32()) for _ in range(CEDB_MAXSORTORDER))
    return DatabaseInfo(oid, name, db_type, db_flags, num_records, num_sort_order, size, modified, specs)
