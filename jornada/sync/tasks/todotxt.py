"""Backend "todotxt": a todo.txt file or a Markdown checklist ⇄ the device's tasks.

One task is one line; its identity is the line's ``id:`` (``🆔``) token. A line
without one is identified by a sha1 prefix of its whitespace-normalized text
and gets that id written in the first time the file is rewritten, so later
edits keep the link (until then an edited line reads as a new task); a line the
sync adds is identified the same way, by the hash of its own text. Every other
line of the file is preserved untouched. With ``done_path`` set, completed
tasks live in that second file (todo.txt's ``done.txt``) and open ones in
``path``; completing or reopening a task moves its line. Writes are atomic
(temporary file, then rename) and a missing file is created.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ...pim.models import Task
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from . import markdown_format, todotxt_format
from .common import Log
from .filters import wrap_completed
from .linetask import LineFormat, ParsedTask, hash_id, unique_id

SERVICE = "todo.txt"
PATH_SETTING, FORMAT_SETTING, DONE_PATH_SETTING = "path", "format", "done_path"
FORMATS: Dict[str, LineFormat] = {todotxt_format.KEY: todotxt_format.FORMAT, markdown_format.KEY: markdown_format.FORMAT}
DEFAULT_FORMAT = todotxt_format.KEY
OPEN_FILE, DONE_FILE = 0, 1


@dataclass(frozen=True)
class TaskLine:
    file_index: int
    line_no: int
    text: str
    parsed: ParsedTask
    task_id: str


@dataclass(frozen=True)
class ScannedFile:
    index: int
    path: Path
    lines: Tuple[str, ...]
    newline: str
    tasks: Tuple[TaskLine, ...]

    def task_at(self, line_no: int) -> Optional[TaskLine]:
        return next((t for t in self.tasks if t.line_no == line_no), None)


# -- file access -------------------------------------------------------------------
def read_lines(path: Path) -> Tuple[Tuple[str, ...], str]:
    """``(lines, newline)`` of a text file; a missing file is empty. Not UTF-8 → StoreError."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return (), "\n"
    except OSError as exc:
        raise StoreError(f"cannot read {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StoreError(f"{path} is not UTF-8 text: {exc}") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = tuple(body.split("\n"))
    return (lines[:-1] if lines and lines[-1] == "" else lines), newline


def write_lines(path: Path, lines: Tuple[str, ...], newline: str = "\n") -> None:
    """Replace ``path`` atomically with ``lines`` (always newline-terminated)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    except OSError as exc:
        raise StoreError(f"cannot write {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(line + newline for line in lines))
        os.replace(tmp_name, path)
    except OSError as exc:
        _discard(tmp_name)
        raise StoreError(f"cannot write {path}: {exc}") from exc
    except BaseException:
        _discard(tmp_name)
        raise


def _discard(tmp_name: str) -> None:
    try:
        os.unlink(tmp_name)
    except OSError:
        pass


# -- the store ---------------------------------------------------------------------
class TodoFileStore:
    """Store protocol over the task lines of one (or two) text files."""

    name = "todotxt"

    def __init__(self, path: Path, fmt: LineFormat = todotxt_format.FORMAT, done_path: Optional[Path] = None,
                 log: Log = lambda _line: None, new_id: Optional[Callable[[], str]] = None,
                 today: Callable[[], date] = date.today) -> None:
        self._paths = (path,) if done_path is None else (path, done_path)
        self._format = fmt
        self._log = log
        self._new_id = new_id
        self._today = today

    @property
    def path(self) -> Path:
        return self._paths[OPEN_FILE]

    @property
    def done_path(self) -> Optional[Path]:
        return self._paths[DONE_FILE] if len(self._paths) > 1 else None

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        return tuple(Item(id=line.task_id, record=line.parsed.task)
                     for scanned in self._scan() for line in scanned.tasks)

    def create(self, record: Task) -> str:
        files = self._scan()
        task_id = self._fresh_id(files, self._format.render(record, "", None, self._today()))
        target = files[self._target_index(record)]
        rendered = self._format.render(record, task_id, None, self._today())
        self._write(target, {}, (rendered,))
        return task_id

    def update(self, item_id: str, record: Task) -> Optional[str]:
        files = self._scan()
        current = self._find(files, item_id)
        rendered = self._format.render(record, item_id, current.parsed, self._today())
        target_index = self._target_index(record)
        if target_index == current.file_index:
            self._write(files[current.file_index], {current.line_no: rendered}, ())
        else:
            self._write(files[current.file_index], {current.line_no: None}, ())
            self._write(files[target_index], {}, (rendered,))
        return None

    def delete(self, item_id: str) -> None:
        files = self._scan()
        current = self._find(files, item_id)
        self._write(files[current.file_index], {current.line_no: None}, ())

    # -- scanning -------------------------------------------------------------
    def _scan(self) -> Tuple[ScannedFile, ...]:
        raw = tuple((index, path, *read_lines(path)) for index, path in enumerate(self._paths))
        parsed = tuple((index, path, lines, newline, tuple((n, self._format.parse(line)) for n, line in enumerate(lines)))
                       for index, path, lines, newline in raw)
        taken: Set[str] = {p.explicit_id for *_, entries in parsed for _, p in entries if p and p.explicit_id}
        assigned: Set[str] = set()
        files = []
        for index, path, lines, newline, entries in parsed:
            tasks = tuple(_task_line(index, lines[n], n, p, taken, assigned) for n, p in entries if p is not None)
            files.append(ScannedFile(index, path, lines, newline, tasks))
        return tuple(files)

    def _find(self, files: Tuple[ScannedFile, ...], item_id: str) -> TaskLine:
        for scanned in files:
            for line in scanned.tasks:
                if line.task_id == item_id:
                    return line
        raise StoreError(f"{SERVICE}: no task with id {item_id!r} in {', '.join(str(p) for p in self._paths)}")

    def _fresh_id(self, files: Tuple[ScannedFile, ...], line: str) -> str:
        """An id no line of the files carries: the hash of the new ``line`` (before its id is filled in)
        or, when a ``new_id`` hook was given, what it supplies; a collision derives a new id deterministically."""
        taken = {task.task_id for scanned in files for task in scanned.tasks}
        candidate = self._new_id() if self._new_id is not None else hash_id(line)
        return unique_id(candidate, taken)

    def _target_index(self, record: Task) -> int:
        return DONE_FILE if self.done_path is not None and record.is_completed else OPEN_FILE

    # -- writing --------------------------------------------------------------
    def _write(self, scanned: ScannedFile, changes: Dict[int, Optional[str]], appended: Tuple[str, ...]) -> None:
        """Rewrite one file: ``changes`` replace (or, with None, drop) lines, ids are made explicit, ``appended`` follow."""
        kept: List[str] = []
        for line_no, text in enumerate(scanned.lines):
            if line_no in changes:
                if changes[line_no] is not None:
                    kept.append(changes[line_no])  # type: ignore[arg-type]
                continue
            task = scanned.task_at(line_no)
            if task is not None and task.parsed.explicit_id is None:
                kept.append(self._format.add_id(text, task.task_id))
            else:
                kept.append(text)
        write_lines(scanned.path, tuple(kept) + appended, scanned.newline)
        self._log(f"{SERVICE}: wrote {scanned.path} ({len(kept) + len(appended)} lines)")


def _task_line(file_index: int, text: str, line_no: int, parsed: ParsedTask, taken: Set[str],
               assigned: Set[str]) -> TaskLine:
    if parsed.explicit_id and parsed.explicit_id not in assigned:
        task_id = parsed.explicit_id
    else:
        task_id = unique_id(hash_id(text), taken | assigned)
        taken.add(task_id)
    assigned.add(task_id)
    return TaskLine(file_index, line_no, text, replace(parsed, task=replace(parsed.task, uid=task_id)), task_id)


# -- backend spec -------------------------------------------------------------------
def format_for(key: Optional[str]) -> LineFormat:
    name = (key or DEFAULT_FORMAT).strip().lower()
    if name not in FORMATS:
        raise AccountError(f"setting {FORMAT_SETTING} must be one of {', '.join(FORMATS)}, not {name!r}")
    return FORMATS[name]


def build(account: Account, secrets_: Dict[str, Any], context: BuildContext) -> Any:
    path_text = (account.setting(PATH_SETTING) or "").strip()
    if not path_text:
        raise AccountError(f"setting {PATH_SETTING} (the file to sync) is required")
    done_text = (account.setting(DONE_PATH_SETTING) or "").strip()
    store = TodoFileStore(Path(path_text).expanduser(), format_for(account.setting(FORMAT_SETTING)),
                          Path(done_text).expanduser() if done_text else None, log=context.log)
    return wrap_completed(store, account, context.log)


BACKEND = BackendSpec(
    key="todotxt",
    title="todo.txt or Markdown checklist file",
    settings=(
        SettingSpec(PATH_SETTING, "the file to sync (created if missing)"),
        SettingSpec(FORMAT_SETTING, "todotxt (default) or markdown (a checklist with Obsidian Tasks fields)",
                    required=False, default=DEFAULT_FORMAT),
        SettingSpec(DONE_PATH_SETTING, "optional second file for completed tasks (todo.txt's done.txt); "
                                       "open tasks stay in path", required=False),
    ),
    build=build,
    notes="One task per line: notes and the private flag do not fit and do not travel; categories become "
          "+project/@context (todo.txt) or #tags (Markdown). A line gets its id: tag (🆔) the first time the sync "
          "writes the file; until then an edited line counts as a new task.",
)

__all__ = ["TodoFileStore", "BACKEND", "build", "FORMATS", "format_for", "read_lines", "write_lines", "TaskLine"]
