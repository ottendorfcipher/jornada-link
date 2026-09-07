"""Logseq backend: the pages or journals of a graph folder on this Mac ⇄ the device's notes.

Items are the Markdown files themselves (id = ``pages/Name.md`` or
``journals/2026_09_06.md``); the body is the file text. Writes are atomic
(temp file + rename) and never leave the graph folder.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ...pim.models import Note
from ...pim.textfiles import unique_filename
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import wall_clock_from_mtime
from .logseq_names import (JOURNALS, KINDS, PAGES, filename_to_journal_title, filename_to_page_name, has_tag,
                           journal_title_to_filename, page_name_to_filename, with_tag)

GRAPH_SETTING = "graph"
KIND_SETTING = "kind"
TAG_SETTING = "tag"
_EXTENSION = ".md"


def write_atomically(path: Path, text: str) -> None:
    """Write ``text`` (UTF-8, LF line ends) to ``path`` via a temp file in the same folder."""
    data = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".jornada-", suffix=".tmp")
    except OSError as exc:
        raise StoreError(f"cannot write in {path.parent}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except OSError as exc:
        _discard(tmp_name)
        raise StoreError(f"could not write {path.name}: {exc}") from exc
    except BaseException:
        _discard(tmp_name)
        raise


def _discard(tmp_name: str) -> None:
    try:
        os.unlink(tmp_name)
    except OSError:
        pass


class LogseqStore:
    """Store protocol over ``<graph>/pages`` or ``<graph>/journals``."""

    name = "logseq"

    def __init__(self, graph: Path, kind: str = PAGES, tag: str = "",
                 log: Callable[[str], None] = lambda _line: None) -> None:
        if kind not in KINDS:
            raise StoreError(f"Logseq kind must be {' or '.join(KINDS)}, not {kind!r}")
        root = Path(graph).expanduser()
        if not root.is_dir():
            raise StoreError(f"Logseq graph folder {root} does not exist")
        self._root = root.resolve()
        self._kind = kind
        self._tag = tag.strip().lstrip("#").strip()
        self._log = log

    @property
    def directory(self) -> Path:
        return self._root / self._kind

    # -- naming ---------------------------------------------------------------
    def _title_of(self, filename: str) -> Optional[str]:
        if self._kind == JOURNALS:
            return filename_to_journal_title(filename)
        return filename_to_page_name(filename) or None

    def _filename_for(self, title: str) -> str:
        try:
            return journal_title_to_filename(title) if self._kind == JOURNALS else page_name_to_filename(title)
        except ValueError as exc:
            raise StoreError(str(exc)) from exc

    def _safe_path(self, item_id: str) -> Path:
        """The graph file behind an item id, refusing anything outside this store's folder."""
        kind, _, filename = item_id.partition("/")
        bad_name = filename in ("", ".", "..") or "/" in filename or "\\" in filename
        if kind != self._kind or bad_name or not filename.lower().endswith(_EXTENSION):
            raise StoreError(f"refusing to touch {item_id!r}: not a {self._kind} file of the graph")
        path = self.directory / filename
        if path.resolve().parent != self.directory.resolve():
            raise StoreError(f"refusing to touch {item_id!r}: it leads outside the graph")
        return path

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        directory = self.directory
        if not directory.is_dir():
            return ()
        items = (self._read(path) for path in sorted(directory.iterdir()))
        return tuple(item for item in items if item is not None)

    def _read(self, path: Path) -> Optional[Item]:
        if not path.is_file() or path.suffix.lower() != _EXTENSION or path.name.startswith("."):
            return None
        title = self._title_of(path.name)
        if title is None:
            self._log(f"skipping {path.name}: not a {self._kind} file name")
            return None
        if path.resolve().parent != self.directory.resolve():
            self._log(f"skipping {path.name}: it leads outside the graph")
            return None
        try:
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            mtime = path.stat().st_mtime
        except (OSError, UnicodeDecodeError) as exc:
            self._log(f"skipping {path.name}: {exc}")
            return None
        if self._tag and not has_tag(text, self._tag):
            return None
        record = Note(title=title, body=text, folder=self._kind, modified=wall_clock_from_mtime(mtime))
        return Item(id=f"{self._kind}/{path.name}", record=record, version=str(mtime))

    def create(self, record: Note) -> str:
        directory = self.directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
            taken = {entry.name for entry in directory.iterdir()}
        except OSError as exc:
            raise StoreError(f"cannot use {directory}: {exc}") from exc
        name = unique_filename(self._filename_for(record.title), taken)
        item_id = f"{self._kind}/{name}"
        write_atomically(self._safe_path(item_id), with_tag(record.body, self._tag))
        return item_id

    def update(self, item_id: str, record: Note) -> Optional[str]:
        path = self._safe_path(item_id)
        if not path.is_file():
            raise StoreError(f"{item_id} is no longer in the graph")
        write_atomically(path, with_tag(record.body, self._tag))
        return None

    def delete(self, item_id: str) -> None:
        path = self._safe_path(item_id)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise StoreError(f"{item_id} is no longer in the graph") from exc
        except OSError as exc:
            raise StoreError(f"could not delete {item_id}: {exc}") from exc


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> LogseqStore:
    graph = (account.setting(GRAPH_SETTING, "") or "").strip()
    if not graph:
        raise AccountError(f"account {account.name!r} needs the {GRAPH_SETTING} setting (path of the Logseq graph)")
    kind = (account.setting(KIND_SETTING, PAGES) or PAGES).strip().lower()
    try:
        return LogseqStore(Path(graph), kind=kind, tag=account.setting(TAG_SETTING, "") or "", log=context.log)
    except StoreError as exc:
        raise AccountError(f"account {account.name!r}: {exc}") from exc


BACKEND = BackendSpec(
    key="logseq",
    title="Logseq graph",
    settings=(
        SettingSpec(GRAPH_SETTING, "path of the Logseq graph folder on this Mac"),
        SettingSpec(KIND_SETTING, f"{PAGES} (default) or {JOURNALS}", required=False, default=PAGES),
        SettingSpec(TAG_SETTING, "only sync pages carrying #tag or tags:: tag (added to pages written by the sync)",
                    required=False),
    ),
    build=build,
    notes="Edits the Markdown files directly; Logseq picks the changes up while it is open.",
)

__all__ = ["LogseqStore", "BACKEND", "build", "write_atomically"]
