"""Apple Notes backend: one folder in the Notes app ⇄ the device's notes, through JXA.

Every operation is one small JavaScript-for-Automation script run by
``osascript``. The scripts are constants: user text (folder names, titles,
note HTML, ids) always travels as ``argv`` entries and is never interpolated
into script source. Each script prints JSON.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from ...pim.models import Note
from ...webapi.applescript import AppleScriptError, Runner, run_jxa
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .apple_html import html_to_text, text_to_html
from .common import wall_clock_from_iso

DEFAULT_NOTES_FOLDER = "Jornada"
FOLDER_SETTING = "notes_folder"   # not "folder": that key is the module's device folder
ACCOUNT_SETTING = "account"

_PRELUDE = """
function notesApp() { return Application("Notes"); }
function accountOf(Notes, accountName) {
  if (!accountName) { return Notes.defaultAccount(); }
  const account = Notes.accounts.byName(accountName);
  if (!account.exists()) { throw new Error("Notes has no account called " + accountName); }
  return account;
}
function folderIn(Notes, folderName, accountName) {
  const owner = accountOf(Notes, accountName);
  const folder = owner.folders.byName(folderName);
  if (folder.exists()) { return folder; }
  owner.folders.push(Notes.Folder({name: folderName}));
  return owner.folders.byName(folderName);
}
function noteById(Notes, id) { return Notes.notes.byId(id); }
function stamp(date) { return date ? new Date(date).toISOString() : ""; }
"""

LIST_SCRIPT = _PRELUDE + """
function run(argv) {
  const Notes = notesApp();
  const notes = folderIn(Notes, argv[0], argv[1]).notes;
  const ids = notes.id(), names = notes.name(), bodies = notes.body(), dates = notes.modificationDate();
  return JSON.stringify(ids.map(function (id, i) {
    return {id: id, name: names[i], body: bodies[i], modified: stamp(dates[i])};
  }));
}
"""

CREATE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Notes = notesApp();
  const folder = folderIn(Notes, argv[0], argv[1]);
  const note = Notes.Note({name: argv[2], body: argv[3]});
  folder.notes.push(note);
  return JSON.stringify({id: note.id(), modified: stamp(note.modificationDate())});
}
"""

UPDATE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Notes = notesApp();
  const note = noteById(Notes, argv[0]);
  if (!note.exists()) { throw new Error("the note is no longer in Notes"); }
  note.body = argv[1];
  return JSON.stringify({id: note.id(), modified: stamp(note.modificationDate())});
}
"""

DELETE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Notes = notesApp();
  const note = noteById(Notes, argv[0]);
  const existed = note.exists();
  if (existed) { note.delete(); }
  return JSON.stringify({id: argv[0], deleted: existed});
}
"""


def _id_of(answer: Any, what: str) -> str:
    if not isinstance(answer, dict) or not isinstance(answer.get("id"), str) or not answer["id"]:
        raise StoreError(f"Apple Notes did not report the id of the note it should {what}")
    return answer["id"]


def _version_of(answer: Any) -> Optional[str]:
    modified = answer.get("modified") if isinstance(answer, dict) else None
    return modified if isinstance(modified, str) and modified else None


class AppleNotesStore:
    """Store protocol over the notes of one Apple Notes folder (created when missing)."""

    name = "apple-notes"

    def __init__(self, folder: str = DEFAULT_NOTES_FOLDER, account: str = "", runner: Optional[Runner] = None,
                 log: Callable[[str], None] = lambda _line: None) -> None:
        self._folder = folder.strip() or DEFAULT_NOTES_FOLDER
        self._account = account.strip()
        self._runner = runner
        self._log = log

    @property
    def folder(self) -> str:
        return self._folder

    def _run(self, script: str, args: Tuple[str, ...], what: str) -> Any:
        try:
            return run_jxa(script, args, runner=self._runner)
        except AppleScriptError as exc:
            raise StoreError(f"Apple Notes could not {what}: {exc}") from exc

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        answer = self._run(LIST_SCRIPT, (self._folder, self._account), f"list the folder {self._folder!r}")
        if not isinstance(answer, list):
            raise StoreError("Apple Notes returned an unexpected listing")
        return tuple(self._item(entry) for entry in answer)

    def _item(self, entry: Any) -> Item:
        note_id = _id_of(entry, "list")
        title = str(entry.get("name") or "")
        body = html_to_text(str(entry.get("body") or ""), title)
        modified = entry.get("modified")
        record = Note(title=title, body=body, folder=self._folder, modified=wall_clock_from_iso(modified), uid=note_id)
        return Item(id=note_id, record=record, version=_version_of(entry))

    def create(self, record: Note) -> str:
        html = text_to_html(record.title, record.body)
        answer = self._run(CREATE_SCRIPT, (self._folder, self._account, record.title, html),
                           f"create the note {record.title!r}")
        return _id_of(answer, "create")

    def update(self, item_id: str, record: Note) -> Optional[str]:
        _check_id(item_id)
        html = text_to_html(record.title, record.body)
        answer = self._run(UPDATE_SCRIPT, (item_id, html), f"update the note {record.title!r}")
        return _version_of(answer)

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        answer = self._run(DELETE_SCRIPT, (item_id,), "delete the note")
        if isinstance(answer, dict) and answer.get("deleted") is False:
            self._log(f"note {item_id} was already gone from Apple Notes")


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip():
        raise StoreError("Apple Notes needs a note id to update or delete")


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> AppleNotesStore:
    return AppleNotesStore(folder=account.setting(FOLDER_SETTING, DEFAULT_NOTES_FOLDER) or DEFAULT_NOTES_FOLDER,
                           account=account.setting(ACCOUNT_SETTING, "") or "",
                           runner=context.runner, log=context.log)


BACKEND = BackendSpec(
    key="apple",
    title="Apple Notes",
    settings=(
        SettingSpec(FOLDER_SETTING, f"folder in Notes to sync (default {DEFAULT_NOTES_FOLDER}; created if missing)",
                    required=False, default=DEFAULT_NOTES_FOLDER),
        SettingSpec(ACCOUNT_SETTING, "Notes account holding the folder (default: the default account)",
                    required=False),
    ),
    build=build,
    notes="Drives the Notes app through osascript; macOS asks for Automation permission on the first run.",
)

__all__ = ["AppleNotesStore", "BACKEND", "build", "html_to_text", "text_to_html",
           "LIST_SCRIPT", "CREATE_SCRIPT", "UPDATE_SCRIPT", "DELETE_SCRIPT"]
