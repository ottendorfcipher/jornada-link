"""Backend "pages": Apple Pages documents in a Mac folder, driven through Pages' own
scripting (JavaScript for Automation via ``osascript``).

Every value — file paths and the whole document text — reaches the scripts as
``argv`` entries, never by interpolation into script source. Deleting moves the
file to the Trash through the Finder; nothing here unlinks files itself.
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from ...pim.models import Document
from ...pim.textfiles import decode_text, safe_filename, unique_filename
from ...webapi.applescript import AppleScriptError, Runner, run_jxa
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .textcodec import DEVICE_FORMAT_SETTING

EXTENSION = ".pages"
Log = Callable[[str], None]

EXPORT_SCRIPT = """
function run(argv) {
  var pages = Application("Pages");
  var doc = pages.open(Path(argv[0]));
  try {
    doc.export({to: Path(argv[1]), as: "unformatted text"});
  } finally {
    doc.close({saving: "no"});
  }
  return JSON.stringify({ok: true});
}
"""

CREATE_SCRIPT = """
function run(argv) {
  var pages = Application("Pages");
  var doc = pages.Document();
  pages.documents.push(doc);
  doc.bodyText = argv[1];
  doc.save({in: Path(argv[0])});
  doc.close({saving: "no"});
  return JSON.stringify({ok: true});
}
"""

UPDATE_SCRIPT = """
function run(argv) {
  var pages = Application("Pages");
  var doc = pages.open(Path(argv[0]));
  try {
    doc.bodyText = argv[1];
    doc.save();
  } finally {
    doc.close({saving: "no"});
  }
  return JSON.stringify({ok: true});
}
"""

TRASH_SCRIPT = """
function run(argv) {
  Application("Finder").delete(Path(argv[0]));
  return JSON.stringify({ok: true});
}
"""


class PagesStore:
    """Store protocol over the ``.pages`` files (packages or single files) of one folder."""

    name = "pages"

    def __init__(self, folder: Path, runner: Optional[Runner] = None, log: Log = lambda _line: None) -> None:
        self._folder = folder
        self._runner = runner
        self._log = log

    @property
    def folder(self) -> Path:
        return self._folder

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        if not self._folder.is_dir():
            self._log(f"{self._folder} does not exist yet; no Pages documents to read")
            return ()
        items = []
        for path in self._documents():
            mtime = path.stat().st_mtime
            record = Document(name=path.stem, text=self._export_text(path), kind="text",
                              modified=datetime.fromtimestamp(mtime))
            items.append(Item(id=path.name, record=record, version=f"{mtime:.0f}"))
        return tuple(items)

    def create(self, record: Any) -> str:
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            taken = {entry.name for entry in self._folder.iterdir()}
        except OSError as exc:
            raise StoreError(f"cannot use {self._folder}: {exc}") from exc
        name = unique_filename(safe_filename(record.name, EXTENSION), taken)
        self._run(CREATE_SCRIPT, [str(self._folder / name), record.text], f"create {name}")
        return name

    def update(self, item_id: str, record: Any) -> Optional[str]:
        path = self._existing(item_id)
        self._run(UPDATE_SCRIPT, [str(path), record.text], f"update {item_id}")
        return None

    def delete(self, item_id: str) -> None:
        path = self._existing(item_id)
        self._run(TRASH_SCRIPT, [str(path)], f"move {item_id} to the Trash")

    # -- helpers ----------------------------------------------------------------
    def _documents(self) -> List[Path]:
        return sorted(p for p in self._folder.glob("*" + EXTENSION) if not p.name.startswith("."))

    def _existing(self, item_id: str) -> Path:
        unsafe = not item_id or "/" in item_id or "\\" in item_id or item_id.startswith(".")
        if unsafe or not item_id.endswith(EXTENSION):
            raise StoreError(f"refusing to touch {item_id!r} outside {self._folder}")
        path = self._folder / item_id
        if not path.exists():
            raise StoreError(f"{path} does not exist")
        return path

    def _export_text(self, path: Path) -> str:
        with tempfile.TemporaryDirectory(prefix="jornada-pages-") as tmp:
            target = Path(tmp) / "export.txt"
            self._run(EXPORT_SCRIPT, [str(path), str(target)], f"read {path.name}")
            try:
                return decode_text(target.read_bytes())
            except OSError as exc:
                raise StoreError(f"Pages did not export {path.name}: {exc}") from exc

    def _run(self, script: str, args: Sequence[str], what: str) -> None:
        try:
            run_jxa(script, args, runner=self._runner)
        except AppleScriptError as exc:
            raise StoreError(f"Pages could not {what}: {exc}") from exc


# -- backend spec ---------------------------------------------------------------
def build(account: Account, secrets: Any, context: BuildContext) -> PagesStore:
    path = (account.setting("path") or "").strip()
    if not path:
        raise AccountError("the pages backend needs the path setting: a Mac folder for the .pages files")
    return PagesStore(Path(path).expanduser(), runner=context.runner, log=context.log)


BACKEND = BackendSpec(
    key="pages",
    title="Apple Pages files in a folder",
    settings=(
        SettingSpec("path", "Mac folder holding the .pages documents (created on first write)"),
        DEVICE_FORMAT_SETTING,
    ),
    build=build,
    notes="Drives Pages through Automation (allow it when macOS asks); deleted documents go to the Trash. "
          "The device folder setting is `folder`; `path` is the Mac folder.",
)
