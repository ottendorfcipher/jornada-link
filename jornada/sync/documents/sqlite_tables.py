"""Tables of a SQLite file on the Mac, one ``Document(kind="table")`` per table.

The CSV header row names the columns; a column is INTEGER or REAL only when
every non-empty value is canonically that kind of number, otherwise TEXT, so
nothing the user typed is reshaped. Identifiers are double-quoted and values
always travel as parameters — never formatted into SQL.
"""
from __future__ import annotations

import time

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple

from ...pim.models import Document
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .csvtext import Rows, normalize_rows, number_text, parse_number, rows_to_csv, table_from_csv

KEY = "sqlite"
KIND = "table"
INTEGER = "INTEGER"
REAL = "REAL"
TEXT = "TEXT"
RESERVED_PREFIX = "sqlite_"
Schema = Tuple[Tuple[str, str], ...]

SETTINGS = (
    SettingSpec("database", "path of the SQLite file (created when missing)"),
    SettingSpec("tables", "comma-separated table names to sync (default: every table)", required=False),
)


# -- SQL helpers -------------------------------------------------------------------
def quote_identifier(name: str) -> str:
    if not name or "\x00" in name:
        raise StoreError(f"bad SQL identifier {name!r}")
    return '"' + name.replace('"', '""') + '"'


def table_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise StoreError("a table needs a name")
    if cleaned.lower().startswith(RESERVED_PREFIX):
        raise StoreError(f"table names starting with {RESERVED_PREFIX!r} are reserved by SQLite")
    return cleaned


def column_type(values: Sequence[str]) -> str:
    """INTEGER / REAL when every non-empty value is canonically such a number, else TEXT."""
    present = [value for value in values if value != ""]
    parsed = [parse_number(value) for value in present]
    if not present or any(number is None for number in parsed):
        return TEXT
    return INTEGER if all(isinstance(number, int) for number in parsed) else REAL


def infer_schema(columns: Sequence[str], rows: Rows) -> Schema:
    return tuple((column, column_type([row[index] for row in rows])) for index, column in enumerate(columns))


def typed_value(text: str, kind: str) -> Any:
    if text == "":
        return None
    if kind == INTEGER:
        return int(text)
    if kind == REAL:
        return float(text)
    return text


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return number_text(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return str(value)


def parse_tables(setting: Optional[str]) -> Tuple[str, ...]:
    return tuple(name.strip() for name in (setting or "").split(",") if name.strip())


# -- the store ---------------------------------------------------------------------
class SqliteTableStore:
    """Store protocol over the tables of one SQLite database file."""

    name = KEY

    def __init__(self, path: Path, tables: Sequence[str] = (), log: Callable[[str], None] = lambda _line: None) -> None:
        self._path = path
        self._tables = tuple(tables)
        self._log = log

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """One connection whose work commits together (or rolls back); SQLite errors become StoreError."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self._path))
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"cannot open {self._path}: {exc}") from exc
        try:
            with connection:
                yield connection
        except sqlite3.Error as exc:
            raise StoreError(f"{self._path.name}: {exc}") from exc
        finally:
            connection.close()

    def _table_names(self, connection: sqlite3.Connection) -> Tuple[str, ...]:
        found = tuple(str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_jornada\\_trash\\_%' ESCAPE '\\' ORDER BY name"))
        if not self._tables:
            return found
        wanted = {name.casefold() for name in self._tables}
        return tuple(name for name in found if name.casefold() in wanted)

    @staticmethod
    def _schema(connection: sqlite3.Connection, table: str) -> Schema:
        info = connection.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
        return tuple((str(row[1]), str(row[2] or TEXT).upper()) for row in info)

    @staticmethod
    def _read(connection: sqlite3.Connection, table: str) -> str:
        cursor = connection.execute(f"SELECT * FROM {quote_identifier(table)}")
        columns = tuple(str(description[0]) for description in cursor.description)
        rows = tuple(tuple(cell_text(value) for value in row) for row in cursor.fetchall())
        return rows_to_csv(normalize_rows((columns,) + rows))

    @staticmethod
    def _create(connection: sqlite3.Connection, table: str, schema: Schema) -> None:
        definitions = ", ".join(f"{quote_identifier(column)} {kind}" for column, kind in schema)
        connection.execute(f"CREATE TABLE {quote_identifier(table)} ({definitions})")

    @staticmethod
    def _insert(connection: sqlite3.Connection, table: str, schema: Schema, rows: Rows) -> None:
        if not rows:
            return
        names = ", ".join(quote_identifier(column) for column, _kind in schema)
        marks = ", ".join("?" for _ in schema)
        values = [tuple(typed_value(cell, kind) for cell, (_column, kind) in zip(row, schema)) for row in rows]
        connection.executemany(f"INSERT INTO {quote_identifier(table)} ({names}) VALUES ({marks})", values)

    @staticmethod
    def _parse(record: Document) -> Tuple[Schema, Rows]:
        try:
            columns, rows = table_from_csv(record.text)
        except ValueError as exc:
            raise StoreError(f"{record.name}: {exc}") from exc
        return infer_schema(columns, rows), rows

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        with self._session() as connection:
            return tuple(Item(id=table, record=Document(name=table, text=self._read(connection, table), kind=KIND))
                         for table in self._table_names(connection))

    def create(self, record: Document) -> str:
        table = table_name(record.name)
        schema, rows = self._parse(record)
        with self._session() as connection:
            if self._schema(connection, table):
                raise StoreError(f"table {table!r} already exists in {self._path.name}")
            self._create(connection, table, schema)
            self._insert(connection, table, schema, rows)
        self._log(f"created table {table} ({len(rows)} rows)")
        return table

    def update(self, item_id: str, record: Document) -> Optional[str]:
        table = table_name(item_id)
        schema, rows = self._parse(record)
        with self._session() as connection:
            existing = self._schema(connection, table)
            if existing != schema:
                connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")
                self._create(connection, table, schema)
            else:
                connection.execute(f"DELETE FROM {quote_identifier(table)}")
            self._insert(connection, table, schema, rows)
        return None

    def delete(self, item_id: str) -> None:
        """Rename the table into the ``_jornada_trash_`` namespace rather than dropping it."""
        table = table_name(item_id)
        trash = f"_jornada_trash_{table}_{time.strftime('%Y%m%d%H%M%S')}"
        with self._session() as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
            if exists:
                connection.execute(f"ALTER TABLE {quote_identifier(table)} RENAME TO {quote_identifier(trash)}")


# -- backend --------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> SqliteTableStore:
    setting = (account.setting("database") or "").strip()
    if not setting:
        raise AccountError("set database to the path of the SQLite file")
    return SqliteTableStore(Path(setting).expanduser(), parse_tables(account.setting("tables")), log=context.log)


BACKEND = BackendSpec(
    key=KEY,
    title="SQLite database tables (Pocket Access)",
    settings=SETTINGS,
    build=build,
    notes="Each table becomes a Windows CE database on the device, as ActiveSync did for Pocket Access.",
)
