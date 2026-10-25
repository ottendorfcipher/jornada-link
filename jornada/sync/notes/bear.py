"""Bear backend: notes are read from Bear's SQLite database and written through its URL scheme.

Bear has no scripting interface for writes, so creates, updates and deletions
are ``bear://x-callback-url`` requests handed to the app with ``/usr/bin/open``.
Those are asynchronous: after a create the database is polled (a few seconds)
for the new note's identifier. The database is only ever opened read-only.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Set, Tuple
from urllib.parse import quote

from ...pim.models import Note
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import core_data_to_wall_clock, drop_title_line, heading_text

DEFAULT_DATABASE = (Path.home() / "Library" / "Group Containers" / "9K33E3U3T4.net.shinyfrog.bear"
                    / "Application Data" / "database.sqlite")
DATABASE_SETTING = "database"
TAG_SETTING = "tag"
OPEN_BINARY = "/usr/bin/open"
POLL_INTERVAL = 0.25
POLL_ATTEMPTS = 12   # about three seconds
_NOTE_ID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_SELECT_NOTES = ("SELECT ZUNIQUEIDENTIFIER, ZTITLE, ZTEXT, ZMODIFICATIONDATE FROM ZSFNOTE "
                 "WHERE COALESCE(ZTRASHED, 0) = 0 AND COALESCE(ZARCHIVED, 0) = 0 "
                 "ORDER BY ZMODIFICATIONDATE, ZUNIQUEIDENTIFIER")

Opener = Callable[[str], None]
Row = Tuple[Any, Any, Any, Any]


# -- URLs (pure) --------------------------------------------------------------------
def _url(action: str, query: Sequence[Tuple[str, str]]) -> str:
    return f"bear://x-callback-url/{action}?" + "&".join(f"{key}={quote(value, safe='')}" for key, value in query)


def create_url(title: str, body: str, tag: str = "") -> str:
    query = [("title", title), ("text", body), ("open_note", "no"), ("show_window", "no")]
    return _url("create", query + ([("tags", tag)] if tag else []))


def update_url(note_id: str, title: str, body: str, tag: str = "") -> str:
    query = [("id", note_id), ("mode", "replace_all"), ("text", heading_text(title, body)),
             ("open_note", "no"), ("show_window", "no")]
    return _url("add-text", query + ([("tags", tag)] if tag else []))


def trash_url(note_id: str) -> str:
    return _url("trash", [("id", note_id), ("show_window", "no")])


def has_tag(text: str, tag: str) -> bool:
    """True when ``text`` carries ``#tag`` (Bear tags are case-insensitive; ``#tag/child`` is another tag)."""
    wanted = tag.strip().lstrip("#").strip()
    if not wanted:
        return True
    return re.search(r"(?<!\S)#" + re.escape(wanted) + r"(?![\w/-])", text, re.IGNORECASE) is not None


def open_url(url: str) -> None:
    """Hand a bear:// URL to Bear in the background."""
    try:
        completed = subprocess.run([OPEN_BINARY, "-g", url], capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreError(f"could not hand the note to Bear: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip() or f"exit status {completed.returncode}"
        raise StoreError(f"could not hand the note to Bear (is it installed?): {detail}")


def _check_id(note_id: str) -> None:
    if not isinstance(note_id, str) or not _NOTE_ID.match(note_id):
        raise StoreError(f"{note_id!r} is not a Bear note identifier")


class BearStore:
    """Store protocol over Bear's notes (optionally those carrying one tag)."""

    name = "bear"

    def __init__(self, database: Path = DEFAULT_DATABASE, tag: str = "", opener: Opener = open_url,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda _line: None) -> None:
        self._database = Path(database).expanduser()
        self._tag = tag.strip().lstrip("#").strip()
        self._opener = opener
        self._sleep = sleep
        self._log = log

    # -- database (read-only) ---------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if not self._database.is_file():
            raise StoreError(f"Bear's database was not found at {self._database}; is Bear installed?")
        uri = f"file:{quote(str(self._database.resolve()))}?mode=ro"
        try:
            return sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error as exc:
            raise StoreError(f"could not open Bear's database: {exc}") from exc

    def _rows(self) -> Tuple[Row, ...]:
        connection = self._connect()
        try:
            return tuple(connection.execute(_SELECT_NOTES).fetchall())
        except sqlite3.Error as exc:
            raise StoreError(f"could not read Bear's notes: {exc}") from exc
        finally:
            connection.close()

    def _row_of(self, note_id: str) -> Optional[Row]:
        for row in self._rows():
            if row[0] == note_id:
                return row
        return None

    # -- Store protocol ---------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        items = (self._item(row) for row in self._rows())
        return tuple(item for item in items if item is not None)

    def _item(self, row: Row) -> Optional[Item]:
        note_id, title, text, modified = row
        if not isinstance(note_id, str) or not note_id:
            return None
        if not isinstance(text, str):
            self._log(f"Bear note {note_id} has no readable text (encrypted?); it is left alone")
            return Item(id=note_id, record=None, problem="no readable text (encrypted?)")
        if self._tag and not has_tag(text, self._tag):
            return None
        title = str(title or "")
        record = Note(title=title, body=drop_title_line(text, title), modified=core_data_to_wall_clock(modified),
                      uid=note_id)
        return Item(id=note_id, record=record, version=None if modified is None else str(modified))

    def create(self, record: Note) -> str:
        known = {row[0] for row in self._rows()}
        self._opener(create_url(record.title, record.body, self._tag))
        for _attempt in range(POLL_ATTEMPTS):
            found = self._find_new(record.title, known)
            if found is not None:
                return found
            self._sleep(POLL_INTERVAL)
        raise StoreError(f"Bear did not create {record.title!r} within {POLL_ATTEMPTS * POLL_INTERVAL:.0f}s; "
                         "make sure Bear is running (it receives the sync's notes through bear:// URLs)")

    def _find_new(self, title: str, known: Set[str]) -> Optional[str]:
        for note_id, row_title, _text, _modified in self._rows():
            if note_id not in known and str(row_title or "").strip() == title.strip():
                return note_id
        return None

    def update(self, item_id: str, record: Note) -> Optional[str]:
        _check_id(item_id)
        self._opener(update_url(item_id, record.title, record.body, self._tag))
        if not self._wait_for_text(item_id, heading_text(record.title, record.body)):
            self._log(f"Bear has not confirmed the update of {record.title!r} yet; it is re-checked on the next sync")
        return None

    def _wait_for_text(self, note_id: str, expected: str) -> bool:
        wanted = expected.rstrip()
        for _attempt in range(POLL_ATTEMPTS):
            row = self._row_of(note_id)
            if row is not None and isinstance(row[2], str) and row[2].rstrip().startswith(wanted):
                return True
            self._sleep(POLL_INTERVAL)
        return False

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        self._opener(trash_url(item_id))


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> BearStore:
    database = Path((account.setting(DATABASE_SETTING, "") or "").strip() or DEFAULT_DATABASE).expanduser()
    if not database.is_file():
        raise AccountError(f"account {account.name!r}: Bear's database was not found at {database}")
    return BearStore(database, tag=account.setting(TAG_SETTING, "") or "",
                     opener=context.extra.get("bear_opener", open_url), sleep=context.extra.get("bear_sleep", time.sleep),
                     log=context.log)


BACKEND = BackendSpec(
    key="bear",
    title="Bear",
    settings=(
        SettingSpec(DATABASE_SETTING, "path of Bear's database.sqlite (default: Bear's own, read-only)", required=False),
        SettingSpec(TAG_SETTING, "only sync notes carrying #tag (added to notes written by the sync)", required=False),
    ),
    build=build,
    notes="Reads Bear's database; writes go through bear:// URLs, so Bear must be running during a sync.",
)

__all__ = ["BearStore", "BACKEND", "build", "create_url", "update_url", "trash_url", "has_tag", "open_url"]
