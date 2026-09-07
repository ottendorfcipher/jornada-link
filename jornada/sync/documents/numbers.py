"""Apple Numbers documents in a Mac folder, driven through JavaScript for Automation.

Numbers itself does the format work: a document is read by exporting it as
CSV, and written by importing a CSV file and saving the result over the
``.numbers`` path. Paths travel as ``osascript`` arguments, never inside the
script text. Deleting moves the file to the Trash through the Finder.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ...pim.models import Document
from ...pim.textfiles import decode_text, safe_filename, unique_filename
from ...webapi.applescript import AppleScriptError, Runner, run_jxa
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .csvtext import Rows, csv_to_rows, normalize_rows, rows_to_csv
from .sheetcodec import KIND

KEY = "numbers"
EXTENSION = ".numbers"
CSV_EXTENSION = ".csv"
EXPORT_NAME = "export.csv"
IMPORT_NAME = "import.csv"
SAVED_NAME = "saved.numbers"
SETTINGS = (SettingSpec("folder", "Mac folder holding the .numbers documents"),)

EXPORT_SCRIPT = """
function run(argv) {
  const source = argv[0], target = argv[1];
  const Numbers = Application('Numbers');
  const doc = Numbers.open(Path(source));
  try {
    doc.export({to: Path(target), as: 'CSV'});
  } finally {
    doc.close({saving: 'no'});
  }
  return JSON.stringify({exported: target});
}
"""

IMPORT_SCRIPT = """
function run(argv) {
  const source = argv[0], target = argv[1];
  const Numbers = Application('Numbers');
  const doc = Numbers.open(Path(source));
  try {
    doc.save({in: Path(target)});
  } finally {
    doc.close({saving: 'no'});
  }
  return JSON.stringify({saved: target});
}
"""

DELETE_SCRIPT = """
function run(argv) {
  Application('Finder').delete(Path(argv[0]));
  return JSON.stringify({deleted: argv[0]});
}
"""


def _stamp(path: Path) -> Tuple[Optional[datetime], str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None, ""
    return datetime.fromtimestamp(mtime), repr(mtime)


def _first_csv(directory: Path) -> Optional[Path]:
    """Numbers writes one CSV file, or a folder of them when the document has several tables."""
    return next(iter(sorted(p for p in directory.rglob("*" + CSV_EXTENSION) if p.is_file())), None)


def _replace(source: Path, target: Path) -> None:
    """Move ``source`` over ``target`` (a single-file or package document)."""
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()
    shutil.move(str(source), str(target))


class NumbersStore:
    """Store protocol over the ``.numbers`` documents of one folder."""

    name = KEY

    def __init__(self, folder: Path, runner: Optional[Runner] = None,
                 log: Callable[[str], None] = lambda _line: None) -> None:
        self._folder = folder
        self._runner = runner
        self._log = log

    def _run(self, script: str, *args: str) -> Any:
        try:
            return run_jxa(script, args, runner=self._runner)
        except AppleScriptError as exc:
            raise StoreError(f"Numbers: {exc}") from exc

    def _target(self, item_id: str) -> Path:
        if Path(item_id).name != item_id or item_id.startswith(".") or not item_id.lower().endswith(EXTENSION):
            raise StoreError(f"Numbers: {item_id!r} is not a document of {self._folder}")
        return self._folder / item_id

    def _documents(self) -> Tuple[Path, ...]:
        try:
            return tuple(sorted(p for p in self._folder.iterdir()
                                if p.name.lower().endswith(EXTENSION) and not p.name.startswith(".")))
        except OSError as exc:
            raise StoreError(f"Numbers: cannot list {self._folder}: {exc}") from exc

    def _export(self, path: Path) -> Rows:
        with tempfile.TemporaryDirectory(prefix="jornada-numbers-") as scratch:
            target = Path(scratch) / EXPORT_NAME
            self._run(EXPORT_SCRIPT, str(path), str(target))
            exported = _first_csv(Path(scratch))
            if exported is None:
                raise StoreError(f"Numbers: exporting {path.name} produced no CSV")
            return normalize_rows(csv_to_rows(decode_text(exported.read_bytes())))

    def _import(self, rows: Rows, target: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="jornada-numbers-") as scratch:
            source = Path(scratch) / IMPORT_NAME
            saved = Path(scratch) / SAVED_NAME
            source.write_text(rows_to_csv(rows), encoding="utf-8")
            self._run(IMPORT_SCRIPT, str(source), str(saved))
            if not saved.exists():
                raise StoreError(f"Numbers: saving {target.name} produced no document")
            try:
                _replace(saved, target)
            except OSError as exc:
                raise StoreError(f"Numbers: cannot write {target}: {exc}") from exc

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        items = []
        for path in self._documents():
            try:
                rows = self._export(path)
            except (StoreError, ValueError) as exc:
                self._log(f"skipping {path.name}: {exc}")
                continue
            modified, version = _stamp(path)
            record = Document(name=path.name[:-len(EXTENSION)], text=rows_to_csv(rows), kind=KIND, modified=modified)
            items.append(Item(id=path.name, record=record, version=version or None))
        return tuple(items)

    def create(self, record: Document) -> str:
        taken = {p.name for p in self._documents()}
        name = unique_filename(safe_filename(record.name, EXTENSION), taken)
        self._import(normalize_rows(csv_to_rows(record.text)), self._folder / name)
        return name

    def update(self, item_id: str, record: Document) -> Optional[str]:
        self._import(normalize_rows(csv_to_rows(record.text)), self._target(item_id))
        return None

    def delete(self, item_id: str) -> None:
        target = self._target(item_id)
        if not target.exists():
            raise StoreError(f"Numbers: {item_id} is not in {self._folder}")
        self._run(DELETE_SCRIPT, str(target))


# -- backend --------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> NumbersStore:
    setting = (account.setting("folder") or "").strip()
    if not setting:
        raise AccountError("set folder to the Mac folder that holds the .numbers documents")
    folder = Path(os.path.expanduser(setting))
    if not folder.is_dir():
        raise AccountError(f"folder {folder} does not exist")
    return NumbersStore(folder, runner=context.runner, log=context.log)


BACKEND = BackendSpec(
    key=KEY,
    title="Apple Numbers files in a folder",
    settings=SETTINGS,
    build=build,
    notes="Numbers must be installed; each document's first table is synced through CSV export/import.",
)
