"""The device side of the SQLite backend: one object-store database per table.

A table named ``T`` is the device database ``<prefix>T`` (ActiveSync used the
bare table name for Pocket Access). Its first record is a schema record whose
only property, ``0x0FF0``, holds the CSV-encoded column names; every other
record is a row whose columns are properties ``0x0001…`` in header order.
Cell text picks the property type: int → i4, float → r8, ISO date/datetime →
filetime, anything else → string; empty cells are simply absent.

The three Pocket Outlook databases are never touched, whatever the prefix, and
a JSON snapshot (the same format as :mod:`jornada.pim.store`) is written
before the first destructive write to any database in a session.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Set, Tuple

from ...cedb import (CEVT_BLOB, CEVT_BOOL, CEVT_FILETIME, CEVT_I2, CEVT_I4, CEVT_LPWSTR, CEVT_R8, CEVT_UI2,
                     CEVT_UI4, PropVal, Record)
from ...constants import CEDB_MAXDBASENAMELEN
from ...pim import ids
from ...pim.codecs import generic
from ...pim.models import Document
from ...pim.store import DeviceStore
from ...pim.timeconv import filetime_to_naive, naive_to_filetime
from ...rapi import RapiClient
from ...rapi_database import DatabaseInfo
from ...rapi_errors import RapiError
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BuildContext
from .csvtext import (Rows, csv_to_rows, header_names, iso_cell_text, iso_to_naive, normalize_rows, number_text,
                      pad_rows, parse_number, rows_to_csv, table_from_csv)

KIND = "table"
SCHEMA_PROP = 0x0FF0
FIRST_COLUMN_PROP = 0x0001
PREFIX_SETTING = "db_prefix"
TYPE_SETTING = "db_type"
SNAPSHOT_DIR = "snapshots"
OUTLOOK_DATABASES = (ids.DB_APPOINTMENTS, ids.DB_CONTACTS, ids.DB_TASKS)
_OUTLOOK = tuple(name.casefold() for name in OUTLOOK_DATABASES)
_I4_MIN, _I4_MAX = -0x8000_0000, 0x7FFF_FFFF
_FILETIME_EPOCH_YEAR = 1601
_INTEGER_KINDS = (CEVT_I2, CEVT_I4, CEVT_UI2, CEVT_UI4)


# -- cells ⇄ properties ------------------------------------------------------------
def cell_prop(prop_id: int, text: str) -> Optional[PropVal]:
    """The property a cell becomes, or None for an empty cell."""
    if text == "":
        return None
    number = parse_number(text)
    if isinstance(number, int):
        return PropVal.i4(prop_id, number) if _I4_MIN <= number <= _I4_MAX else PropVal.string(prop_id, text)
    if isinstance(number, float):
        return PropVal.r8(prop_id, number)
    moment = iso_to_naive(text)
    if moment is not None and moment.year >= _FILETIME_EPOCH_YEAR:
        return PropVal.filetime(prop_id, naive_to_filetime(moment))
    return PropVal.string(prop_id, text)


def prop_text(prop: Optional[PropVal]) -> str:
    if prop is None or prop.value is None:
        return ""
    if prop.kind == CEVT_LPWSTR:
        return str(prop.value)
    if prop.kind in _INTEGER_KINDS:
        return str(int(prop.value))
    if prop.kind == CEVT_R8:
        return number_text(float(prop.value))
    if prop.kind == CEVT_BOOL:
        return "TRUE" if prop.value else "FALSE"
    if prop.kind == CEVT_FILETIME:
        moment = filetime_to_naive(int(prop.value))
        return iso_cell_text(moment) if isinstance(moment, datetime) else ""
    if prop.kind == CEVT_BLOB:
        return bytes(prop.value).hex()
    return str(prop.value)


def schema_prop(columns: Sequence[str]) -> PropVal:
    return PropVal.string(SCHEMA_PROP, rows_to_csv((tuple(columns),)).rstrip("\n"))


def row_props(row: Sequence[str]) -> Tuple[PropVal, ...]:
    props = (cell_prop(FIRST_COLUMN_PROP + index, cell) for index, cell in enumerate(row))
    return tuple(prop for prop in props if prop is not None)


def _schema_columns(records: Sequence[Record]) -> Tuple[Optional[Record], Tuple[str, ...]]:
    schema = next((record for record in records if record.get(SCHEMA_PROP) is not None), None)
    if schema is None:
        return None, ()
    rows = csv_to_rows(str(schema.value(SCHEMA_PROP) or ""))
    return schema, tuple(rows[0]) if rows else ()


def decode_records(records: Sequence[Record]) -> Tuple[Tuple[str, ...], Rows]:
    """Device records → (column names, rows). Columns beyond the schema get ``cNNNN`` names."""
    schema, header = _schema_columns(records)
    data = tuple(record for record in records if record is not schema)
    known = tuple(range(FIRST_COLUMN_PROP, FIRST_COLUMN_PROP + len(header)))
    extra = sorted({p.prop_id for r in data for p in r.props if p.prop_id not in known and p.prop_id != SCHEMA_PROP})
    columns = header_names(header + tuple(f"c{prop_id:04d}" for prop_id in extra))
    order = known + tuple(extra)
    rows = tuple(tuple(prop_text(record.get(prop_id)) for prop_id in order) for record in data)
    return columns, rows


def records_to_csv(records: Sequence[Record]) -> str:
    columns, rows = decode_records(records)
    return rows_to_csv(normalize_rows((columns,) + rows)) if columns else ""


# -- the store ---------------------------------------------------------------------
class DeviceTableStore:
    """Store protocol over the ``<prefix><table>`` databases of the device."""

    name = "device"

    def __init__(self, client: RapiClient, prefix: str = "", db_type: int = 0, snapshot_dir: Optional[Path] = None,
                 log: Callable[[str], None] = lambda _line: None) -> None:
        self._client = client
        self._prefix = prefix
        self._db_type = db_type
        self._snapshot_dir = snapshot_dir
        self._log = log
        self._snapshotted: Set[str] = set()

    # -- names ----------------------------------------------------------------
    def _database_name(self, table: str) -> str:
        cleaned = table.strip()
        if not cleaned:
            raise StoreError("a table needs a name")
        name = self._prefix + cleaned
        if cleaned.casefold() in _OUTLOOK or name.casefold() in _OUTLOOK:
            raise StoreError(f"refusing to touch the Pocket Outlook database {name!r}")
        if len(name) > CEDB_MAXDBASENAMELEN:
            raise StoreError(f"database name {name!r} is longer than {CEDB_MAXDBASENAMELEN} characters")
        return name

    def _table_of(self, info: DatabaseInfo) -> Optional[str]:
        if info.name.casefold() in _OUTLOOK or not info.name.casefold().startswith(self._prefix.casefold()):
            return None
        table = info.name[len(self._prefix):]
        return table if table.strip() else None

    def _databases(self) -> Dict[str, DatabaseInfo]:
        found = {}
        for info in self._client.find_all_databases(self._db_type):
            table = self._table_of(info)
            if table is not None:
                found[table] = info
        return found

    def _find(self, table: str) -> DatabaseInfo:
        name = self._database_name(table)
        info = self._client.find_database(name)
        if info is None:
            raise StoreError(f"the device has no database called {name!r}")
        return info

    # -- records --------------------------------------------------------------
    def _records(self, info: DatabaseInfo) -> Tuple[Record, ...]:
        handle = self._client.open_database(info.oid)
        try:
            return tuple(self._client.iter_records(handle))
        finally:
            self._client.close_handle(handle)

    def _write_all(self, oid: int, columns: Sequence[str], rows: Rows, replace: bool) -> int:
        """Write the schema record and one record per non-empty row; ``replace`` clears the database first."""
        handle = self._client.open_database(oid)
        try:
            if replace:
                for record_oid in [record.oid for record in self._client.iter_records(handle)]:
                    self._client.delete_record(handle, record_oid)
            self._client.write_record(handle, (schema_prop(columns),))
            written = 0
            for row in rows:
                props = row_props(row)
                if props:
                    self._client.write_record(handle, props)
                    written += 1
            return written
        finally:
            self._client.close_handle(handle)

    @staticmethod
    def _parse(record: Document) -> Tuple[Tuple[str, ...], Rows]:
        try:
            columns, rows = table_from_csv(record.text)
        except ValueError as exc:
            raise StoreError(f"{record.name}: {exc}") from exc
        return columns, pad_rows(rows, len(columns))

    # -- safety net -----------------------------------------------------------
    def _ensure_snapshot(self, database: str) -> None:
        if database in self._snapshotted or self._snapshot_dir is None:
            return
        DeviceStore(self._client, generic(database), snapshot_dir=self._snapshot_dir, log=self._log).snapshot()
        self._snapshotted.add(database)

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        items = []
        for table, info in sorted(self._databases().items()):
            try:
                text = records_to_csv(self._records(info))   # a RapiError aborts the run: absence must not read as deletion
            except ValueError as exc:
                if not self._prefix and not self._db_type:
                    continue   # with no prefix, an unrelated device database is simply not ours
                self._log(f"database {info.name!r} cannot be read as a table ({exc}); it is left alone")
                items.append(Item(id=table, record=None, problem=str(exc)))
                continue
            items.append(Item(id=table, record=Document(name=table, text=text, kind=KIND)))
        return tuple(items)

    def create(self, record: Document) -> str:
        table = record.name.strip()
        name = self._database_name(table)
        columns, rows = self._parse(record)
        if self._client.find_database(name) is not None:
            raise StoreError(f"the device already has a database called {name!r}")
        oid = self._client.create_database(name, self._db_type)
        written = self._write_all(oid, columns, rows, replace=False)
        self._log(f"created database {name!r} ({written} rows)")
        return table

    def update(self, item_id: str, record: Document) -> Optional[str]:
        info = self._find(item_id)
        columns, rows = self._parse(record)
        self._ensure_snapshot(info.name)
        self._write_all(info.oid, columns, rows, replace=True)
        return None

    def delete(self, item_id: str) -> None:
        info = self._find(item_id)
        self._ensure_snapshot(info.name)
        self._client.delete_database(info.oid)
        self._log(f"deleted database {info.name!r}")


def device_table_store(client: RapiClient, account: Account, context: BuildContext) -> DeviceTableStore:
    """The store an account configures: ``db_prefix`` (default none) and ``db_type`` (default 0)."""
    raw_type = (account.setting(TYPE_SETTING) or "0").strip()
    try:
        db_type = int(raw_type, 0)
    except ValueError:
        raise StoreError(f"{TYPE_SETTING} must be a number, not {raw_type!r}") from None
    return DeviceTableStore(client, prefix=account.setting(PREFIX_SETTING) or "", db_type=db_type,
                            snapshot_dir=context.sync_dir / SNAPSHOT_DIR, log=context.log)
