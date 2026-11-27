"""What the todo.txt and Markdown checklist grammars share: one parsed task line, the
format protocol the file store drives, and the identity of lines without an id tag."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional, Set, Tuple

from ...pim.models import Task
from ...pim.tasks import UNKNOWN_COMPLETION_DATE
from .common import is_real_completion

ID_LENGTH = 8
_WHITESPACE = re.compile(r"\s+")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ParsedTask:
    """A task line as read from a file: the record plus everything a rewrite must keep."""

    task: Task
    explicit_id: Optional[str] = None
    letter: Optional[str] = None                     # todo.txt priority letter as written
    contexts: Tuple[str, ...] = ()                   # categories written as @context (todo.txt)
    extras: Tuple[Tuple[str, str], ...] = ()         # other key:value tags, in order (todo.txt)
    indent: str = ""                                 # leading whitespace (Markdown)
    bullet: str = "-"                                # list marker (Markdown)
    tail: Tuple[str, ...] = ()                       # fields to keep verbatim (Markdown)


@dataclass(frozen=True)
class LineFormat:
    """How one file format reads and writes task lines."""

    key: str
    parse: Callable[[str], Optional[ParsedTask]]
    render: Callable[[Task, str, Optional[ParsedTask], date], str]
    add_id: Callable[[str, str], str]


def hash_id(line: str) -> str:
    """The identity of a line without an id tag: sha1 of its whitespace-normalized text, 8 hex digits."""
    normalized = _WHITESPACE.sub(" ", line.strip())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:ID_LENGTH]


def unique_id(candidate: str, taken: Set[str]) -> str:
    """``candidate`` or, when taken, a derived id (deterministic: the same collision gives the same id)."""
    chosen, attempt = candidate, 0
    while chosen in taken:
        attempt += 1
        chosen = hashlib.sha1(f"{candidate}#{attempt}".encode("utf-8")).hexdigest()[:ID_LENGTH]
    return chosen


def parse_date(text: Optional[str]) -> Optional[date]:
    """``YYYY-MM-DD`` → date, None for anything else (an invalid calendar date included).

    The shape is checked first so every Python version reads a line the same way
    (``fromisoformat`` on 3.11+ would also accept week dates such as ``2026-W36-7``).
    """
    if not text or not _ISO_DATE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def completion_text(completed: Optional[date], today: date) -> str:
    """The completion date to write: the real one, or today for the unknown-day marker."""
    day = completed if is_real_completion(completed) else today
    return (day or today).isoformat()


def token_of(category: str) -> str:
    """A category as one token (tags cannot hold whitespace)."""
    return _WHITESPACE.sub("_", category.strip())


def words_of(text: str) -> Tuple[str, ...]:
    return tuple(text.split())


__all__ = ["ParsedTask", "LineFormat", "hash_id", "unique_id", "parse_date", "completion_text", "token_of",
           "words_of", "UNKNOWN_COMPLETION_DATE", "ID_LENGTH"]
