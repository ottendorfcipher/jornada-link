import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from jornada.pim.models import Note
from jornada.pim.timeconv import to_wall_clock
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.notes import bear
from jornada.sync.notes.bear import BearStore, create_url, has_tag, open_url, trash_url, update_url
from jornada.sync.notes.common import core_data_to_wall_clock
from jornada.sync.registry import BuildContext

SCHEMA = ("CREATE TABLE ZSFNOTE (Z_PK INTEGER PRIMARY KEY, ZARCHIVED INTEGER, ZTRASHED INTEGER, "
          "ZMODIFICATIONDATE FLOAT, ZTEXT VARCHAR, ZTITLE VARCHAR, ZUNIQUEIDENTIFIER VARCHAR)")


def insert(db: Path, uid, title, text, modified=800000000.0, trashed=0, archived=0):
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO ZSFNOTE (ZARCHIVED, ZTRASHED, ZMODIFICATIONDATE, ZTEXT, ZTITLE, ZUNIQUEIDENTIFIER)"
                           " VALUES (?, ?, ?, ?, ?, ?)", (archived, trashed, modified, text, title, uid))


def set_text(db: Path, uid, text):
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE ZSFNOTE SET ZTEXT = ?, ZMODIFICATIONDATE = ZMODIFICATIONDATE + 1 "
                           "WHERE ZUNIQUEIDENTIFIER = ?", (text, uid))


@pytest.fixture
def database(tmp_path):
    db = tmp_path / "Application Data" / "database.sqlite"      # a space in the path, like Bear's real one
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute(SCHEMA)
    insert(db, "AAA-1", "Shopping", "# Shopping\nMilk\nEggs #jornada", 800000000.0)
    insert(db, "BBB-2", "Old", "# Old\ngone", trashed=1)
    insert(db, "CCC-3", "Archived", "# Archived\nstored", archived=1)
    insert(db, "DDD-4", "Plain", "Plain\nno hash heading", 700000000.0)
    insert(db, "EEE-5", "Locked", None)
    return db


def test_list_filters_trashed_and_archived_and_strips_the_heading(database):
    lines = []
    items = BearStore(database, log=lines.append).list()
    assert [item.id for item in items] == ["DDD-4", "AAA-1"]        # by modification date
    shopping = items[1]
    assert shopping.record == Note("Shopping", "Milk\nEggs #jornada", modified=core_data_to_wall_clock(800000000.0),
                                   uid="AAA-1")
    assert shopping.version == "800000000.0" and shopping.record.modified is not None
    assert items[0].record.body == "no hash heading"
    assert any("EEE-5" in line for line in lines)
    assert core_data_to_wall_clock(0) == to_wall_clock(datetime(2001, 1, 1, tzinfo=timezone.utc))
    assert core_data_to_wall_clock(None) is None and core_data_to_wall_clock("x") is None
    assert BearStore(database).name == "bear"


def test_tag_filter(database):
    assert [item.id for item in BearStore(database, tag="jornada").list()] == ["AAA-1"]
    assert [item.id for item in BearStore(database, tag="#Jornada").list()] == ["AAA-1"]
    assert [item.id for item in BearStore(database, tag="work").list()] == []
    assert has_tag("a #tag, b", "tag") and has_tag("#tag", "tag") and has_tag("x", "")
    assert not has_tag("x #tag/child", "tag") and not has_tag("a#tag", "tag") and not has_tag("#tagged", "tag")


def test_create_polls_until_the_note_appears(database):
    urls, naps = [], []

    def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:            # Bear answers a little later
            query = parse_qs(urlsplit(urls[0]).query)
            insert(database, "NEW-9", query["title"][0], f"# {query['title'][0]}\n{query['text'][0]}")

    store = BearStore(database, tag="jornada", opener=urls.append, sleep=sleep)
    assert store.create(Note("Trip", "pack & go")) == "NEW-9"
    assert urls == [create_url("Trip", "pack & go", "jornada")]
    assert urls[0] == ("bear://x-callback-url/create?title=Trip&text=pack%20%26%20go&open_note=no&show_window=no"
                       "&tags=jornada")
    assert naps == [bear.POLL_INTERVAL, bear.POLL_INTERVAL]
    assert "NEW-9" in [item.id for item in BearStore(database).list()]


def test_create_fails_when_bear_never_answers(database):
    urls, naps = [], []
    store = BearStore(database, opener=urls.append, sleep=naps.append)
    with pytest.raises(StoreError) as info:
        store.create(Note("Trip", "pack"))
    assert "Bear is running" in str(info.value) and "Trip" in str(info.value)
    assert len(naps) == bear.POLL_ATTEMPTS and sum(naps) == pytest.approx(3.0)
    assert urls == ["bear://x-callback-url/create?title=Trip&text=pack&open_note=no&show_window=no"]


def test_update_and_delete_open_the_right_urls(database):
    urls, lines = [], []
    store = BearStore(database, opener=urls.append, sleep=lambda _seconds: None, log=lines.append)
    assert store.update("AAA-1", Note("Shopping", "Milk\nBread")) is None
    assert urls == [update_url("AAA-1", "Shopping", "Milk\nBread")]
    assert urls[0] == ("bear://x-callback-url/add-text?id=AAA-1&mode=replace_all&text=%23%20Shopping%0AMilk%0ABread"
                       "&open_note=no&show_window=no")
    query = parse_qs(urlsplit(urls[0]).query)
    assert query["text"] == ["# Shopping\nMilk\nBread"] and query["mode"] == ["replace_all"]
    assert lines and "not confirmed" in lines[0]                  # nothing applied the change
    assert update_url("AAA-1", "T", "b", "jornada").endswith("&show_window=no&tags=jornada")
    store.delete("AAA-1")
    assert urls[1] == trash_url("AAA-1") == "bear://x-callback-url/trash?id=AAA-1&show_window=no"
    for bad in ("../x", "bad id", "", "a b"):
        with pytest.raises(StoreError):
            store.update(bad, Note("x"))
        with pytest.raises(StoreError):
            store.delete(bad)
    assert len(urls) == 2


def test_update_waits_for_bear_to_apply_the_text(database):
    lines, naps = [], []

    def opener(url):
        query = parse_qs(urlsplit(url).query)
        set_text(database, query["id"][0], query["text"][0] + "\n#jornada")

    store = BearStore(database, opener=opener, sleep=naps.append, log=lines.append)
    store.update("AAA-1", Note("Shopping", "Milk\nBread"))
    assert lines == [] and naps == []
    assert BearStore(database).list()[-1].record.body == "Milk\nBread\n#jornada"


def test_missing_database_and_open_failures(tmp_path, monkeypatch):
    store = BearStore(tmp_path / "nope.sqlite", opener=lambda _url: None, sleep=lambda _s: None)
    with pytest.raises(StoreError) as info:
        store.list()
    assert "not found" in str(info.value)
    with pytest.raises(StoreError):
        store.create(Note("x"))
    broken = tmp_path / "broken.sqlite"
    broken.write_text("not a database", encoding="utf-8")
    with pytest.raises(StoreError):
        BearStore(broken).list()
    monkeypatch.setattr(bear, "OPEN_BINARY", "/nonexistent/open")
    with pytest.raises(StoreError):
        open_url("bear://x-callback-url/trash?id=x")
    if Path("/usr/bin/false").exists():
        monkeypatch.setattr(bear, "OPEN_BINARY", "/usr/bin/false")
        with pytest.raises(StoreError) as info:
            open_url("bear://x-callback-url/trash?id=x")
        assert "installed" in str(info.value)


def test_backend_build_and_settings(database, tmp_path):
    opened = []
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           extra={"bear_opener": opened.append, "bear_sleep": lambda _s: None})
    account = Account("b", "notes", "bear", (("database", str(database)), ("tag", "jornada")))
    store = bear.build(account, {}, context)
    assert [item.id for item in store.list()] == ["AAA-1"]
    store.delete("AAA-1")
    assert opened == [trash_url("AAA-1")]
    with pytest.raises(AccountError):
        bear.build(Account("b", "notes", "bear", (("database", str(tmp_path / "nope")),)), {}, context)
    assert bear.BACKEND.missing_settings(Account("b", "notes", "bear"), {}) == ()
    assert [s.key for s in bear.BACKEND.settings] == ["database", "tag"]
    assert str(bear.DEFAULT_DATABASE).endswith("9K33E3U3T4.net.shinyfrog.bear/Application Data/database.sqlite")
