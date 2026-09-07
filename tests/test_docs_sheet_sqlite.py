import sqlite3

import pytest

from jornada.pim.models import Document
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.documents import sqlite_tables
from jornada.sync.documents.sqlite_tables import SqliteTableStore
from jornada.sync.registry import BuildContext

CSV = "Name,Qty,Price,Code,When,Empty\nWidget,7,1.5,007,2026-01-02,\n\"Gadget, big\",-3,2,x9,,\n"


def schema(path, table):
    with sqlite3.connect(str(path)) as conn:
        return [(row[1], row[2]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def test_create_infers_types_and_list_reads_the_same_csv(tmp_path):
    log = []
    store = SqliteTableStore(tmp_path / "db" / "data.sqlite", log=log.append)
    assert store.list() == ()
    assert store.create(Document("Stock", CSV, "table")) == "Stock"
    assert schema(tmp_path / "db" / "data.sqlite", "Stock") == [
        ("Name", "TEXT"), ("Qty", "INTEGER"), ("Price", "REAL"), ("Code", "TEXT"), ("When", "TEXT"), ("Empty", "TEXT")]
    (item,) = store.list()
    assert item.id == "Stock" and item.version is None
    assert item.record == Document("Stock", "Name,Qty,Price,Code,When,Empty\nWidget,7,1.5,007,2026-01-02\n\"Gadget, big\",-3,2,x9\n", "table")
    assert any("created table Stock (2 rows)" in line for line in log)
    with pytest.raises(StoreError):
        store.create(Document("Stock", CSV, "table"))
    with pytest.raises(StoreError):
        store.create(Document("Empty", "", "table"))
    with pytest.raises(StoreError):
        store.create(Document("   ", CSV, "table"))
    with pytest.raises(StoreError):
        store.create(Document("sqlite_secret", CSV, "table"))


def test_odd_identifiers_are_quoted_safely(tmp_path):
    store = SqliteTableStore(tmp_path / "odd.db")
    table = 'Odd "Table"; DROP TABLE x --'
    text = 'a"b,select,,a"b\n1,2,3,4\n'
    assert store.create(Document(table, text, "table")) == table
    (item,) = store.list()
    assert item.id == table and item.record.text == '"a""b",select,c0003,"a""b_2"\n1,2,3,4\n'
    store.update(table, Document(table, 'x;y,"z""z"\n"it\'s",--\n', "table"))
    (item,) = store.list()
    assert item.record.text == 'x;y,"z""z"\nit\'s,--\n'
    store.delete(table)
    assert store.list() == ()
    with pytest.raises(StoreError):
        sqlite_tables.quote_identifier("")


def test_update_replaces_rows_or_recreates_on_schema_change(tmp_path):
    path = tmp_path / "data.sqlite"
    store = SqliteTableStore(path)
    store.create(Document("T", "a,b\n1,x\n2,y\n", "table"))
    assert store.update("T", Document("T", "a,b\n3,z\n", "table")) is None
    assert store.list()[0].record.text == "a,b\n3,z\n" and schema(path, "T") == [("a", "INTEGER"), ("b", "TEXT")]
    store.update("T", Document("T", "a,b,c\nq,1.5,\n", "table"))
    assert schema(path, "T") == [("a", "TEXT"), ("b", "REAL"), ("c", "TEXT")]
    assert store.list()[0].record.text == "a,b,c\nq,1.5\n"
    store.update("T", Document("T", "a,b,c\n", "table"))
    assert store.list()[0].record.text == "a,b,c\n"
    store.update("Fresh", Document("Fresh", "k\n1\n", "table"))  # a missing table is created
    assert [i.id for i in store.list()] == ["Fresh", "T"]
    with pytest.raises(StoreError):
        store.update("T", Document("T", "", "table"))
    store.delete("T")
    store.delete("T")  # idempotent
    assert [i.id for i in store.list()] == ["Fresh"]


def test_tables_setting_filters_and_existing_data_is_read(tmp_path):
    path = tmp_path / "data.sqlite"
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, height REAL, photo BLOB, born)")
        conn.execute("INSERT INTO people VALUES (1, 'Ada', 1.7, X'CAFE', '1815-12-10')")
        conn.execute("INSERT INTO people VALUES (2, NULL, 2.0, NULL, NULL)")
        conn.execute("CREATE TABLE other (x)")
    everything = SqliteTableStore(path)
    assert [i.id for i in everything.list()] == ["other", "people"]
    people = SqliteTableStore(path, ("PEOPLE", "missing"))
    (item,) = people.list()
    assert item.record.text == "id,name,height,photo,born\n1,Ada,1.7,cafe,1815-12-10\n2,,2\n"
    assert sqlite_tables.parse_tables(" people, ,other ") == ("people", "other")
    assert sqlite_tables.parse_tables(None) == ()


def test_helpers_and_failures(tmp_path):
    assert sqlite_tables.column_type(("1", "", "-2")) == "INTEGER"
    assert sqlite_tables.column_type(("1", "1.5")) == "REAL" and sqlite_tables.column_type(("1.5", "x")) == "TEXT"
    assert sqlite_tables.column_type(("",)) == "TEXT" and sqlite_tables.column_type(("007",)) == "TEXT"
    assert sqlite_tables.typed_value("", "INTEGER") is None and sqlite_tables.typed_value("2", "REAL") == 2.0
    assert sqlite_tables.cell_text(None) == "" and sqlite_tables.cell_text(b"\x01") == "01"
    assert sqlite_tables.cell_text(2.5) == "2.5" and sqlite_tables.cell_text(True) == "TRUE"
    assert sqlite_tables.infer_schema(("a", "b"), (("1", "x"),)) == (("a", "INTEGER"), ("b", "TEXT"))
    directory = tmp_path / "dir.sqlite"
    directory.mkdir()
    with pytest.raises(StoreError):
        SqliteTableStore(directory).list()
    (tmp_path / "junk.sqlite").write_bytes(b"this is not a database at all, just bytes" * 3)
    with pytest.raises(StoreError):
        SqliteTableStore(tmp_path / "junk.sqlite").list()


def test_backend_spec_and_build(tmp_path):
    assert sqlite_tables.BACKEND.key == "sqlite" and sqlite_tables.BACKEND.login is None
    assert [s.key for s in sqlite_tables.BACKEND.settings] == ["database", "tables"]
    context = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    account = Account("a", "documents", "sqlite", (("database", str(tmp_path / "x.db")), ("tables", "a,b")))
    built = sqlite_tables.build(account, {}, context)
    assert isinstance(built, SqliteTableStore) and built.list() == () and (tmp_path / "x.db").exists()
    with pytest.raises(AccountError):
        sqlite_tables.build(Account("a", "documents", "sqlite"), {}, context)
    assert sqlite_tables.BACKEND.missing_settings(Account("a", "documents", "sqlite"), {}) == ("database",)
