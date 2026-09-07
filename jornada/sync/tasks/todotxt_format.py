"""The todo.txt line grammar (todotxt.org) as the file backend reads and writes it.

``x 2026-09-08 2026-09-07 (A) Buy milk +project @home due:2026-09-10 id:k7d2``

* ``x`` + completion date → completed (``x`` alone: completed on an unknown day)
* ``(A)``/``(B)`` → high, none or ``(C)`` → normal, ``(D)``–``(Z)`` → low; written back as
  ``(A)`` / ``(D)`` unless the line already had a letter of the same level
* creation date → start, ``due:`` → due, ``+project`` and ``@context`` → categories
  (re-emitted with the sigil they had; new ones get ``+``), other ``key:value``
  tags are kept, ``id:`` is the identity.
"""
from __future__ import annotations

import re
from datetime import date
from typing import List, Optional, Tuple

from ...pim.models import Task
from .common import NO_SUBJECT
from .linetask import (UNKNOWN_COMPLETION_DATE, LineFormat, ParsedTask, completion_text, parse_date, token_of,
                       words_of)

KEY = "todotxt"
DUE_TAG, ID_TAG, PRIORITY_TAG = "due", "id", "pri"
HIGH_LETTERS = ("A", "B")
NORMAL_LETTERS = ("C",)
DEFAULT_LETTERS = {"high": "A", "low": "D"}
_DATE = r"\d{4}-\d{2}-\d{2}"
_DONE = re.compile(
    rf"^x\s+(?:\((?P<pri1>[A-Z])\)\s+)?(?:(?P<completed>{_DATE})\s+)?(?:(?P<created>{_DATE})\s+)?"
    rf"(?:\((?P<pri2>[A-Z])\)\s+)?(?P<rest>.*)$"
)
_OPEN = re.compile(rf"^(?:\((?P<pri1>[A-Z])\)\s+)?(?:(?P<created>{_DATE})\s+)?(?P<rest>.*)$")
_TAG = re.compile(r"^(?P<key>[A-Za-z][\w.-]*):(?P<value>\S+)$")


def priority_of_letter(letter: Optional[str]) -> str:
    if not letter or letter in NORMAL_LETTERS:
        return "normal"
    return "high" if letter in HIGH_LETTERS else "low"


def letter_for(priority: str, previous: Optional[str]) -> Optional[str]:
    """The letter to write: the previous one when it means the same level, else the default."""
    if previous and priority_of_letter(previous) == priority:
        return previous
    return DEFAULT_LETTERS.get(priority)


def parse_line(text: str) -> Optional[ParsedTask]:
    """One line → ParsedTask; blank lines are not tasks."""
    stripped = text.strip()
    if not stripped:
        return None
    done = _DONE.match(stripped)
    match = done or _OPEN.match(stripped)
    assert match is not None  # the open grammar accepts any text
    groups = match.groupdict()
    completed = parse_date(groups.get("completed")) if done else None
    created, rest = _created_and_rest(groups.get("created"), groups["rest"])
    words, projects, contexts, tags = _split_rest(rest)
    letter = groups.get("pri1") or groups.get("pri2") or _tag_letter(tags)
    task = Task(
        summary=" ".join(words),
        due=parse_date(_tag(tags, DUE_TAG)),
        start=created,
        completed=(completed or UNKNOWN_COMPLETION_DATE) if done else None,
        priority=priority_of_letter(letter),
        categories=projects + contexts,
    )
    extras = tuple((k, v) for k, v in tags if k not in (DUE_TAG, ID_TAG, PRIORITY_TAG))
    return ParsedTask(task, explicit_id=_tag(tags, ID_TAG), letter=letter, contexts=contexts, extras=extras)


def _created_and_rest(created_text: Optional[str], rest: str) -> Tuple[Optional[date], str]:
    created = parse_date(created_text)
    if created_text and created is None:  # looked like a date but is not one: keep the text
        return None, f"{created_text} {rest}"
    return created, rest


def _split_rest(rest: str) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
    words: List[str] = []
    projects: List[str] = []
    contexts: List[str] = []
    tags: List[Tuple[str, str]] = []
    for token in rest.split():
        tag = _TAG.match(token)
        if token.startswith("+") and len(token) > 1:
            projects.append(token[1:])
        elif token.startswith("@") and len(token) > 1:
            contexts.append(token[1:])
        elif tag and "://" not in token:
            tags.append((tag.group("key"), tag.group("value")))
        else:
            words.append(token)
    return tuple(words), tuple(projects), tuple(contexts), tuple(tags)


def _tag(tags: Tuple[Tuple[str, str], ...], key: str) -> Optional[str]:
    return next((value for k, value in tags if k == key), None)


def _tag_letter(tags: Tuple[Tuple[str, str], ...]) -> Optional[str]:
    value = (_tag(tags, PRIORITY_TAG) or "").upper()
    return value if len(value) == 1 and "A" <= value <= "Z" else None


def render_line(task: Task, task_id: str, previous: Optional[ParsedTask], today: date) -> str:
    """Task → one todo.txt line carrying ``id:task_id``; ``previous`` supplies what must be kept."""
    item = task.normalized()
    letter = letter_for(item.priority, previous.letter if previous else None)
    parts: List[str] = []
    if item.is_completed:
        parts += ["x", completion_text(item.completed, today)]
        parts += [item.start.isoformat()] if item.start else []
        parts += [f"({letter})"] if letter else []
    else:
        parts += [f"({letter})"] if letter else []
        parts += [item.start.isoformat()] if item.start else []
    parts += words_of(item.summary or NO_SUBJECT)
    contexts = set(previous.contexts) if previous else set()
    parts += [("@" if category in contexts else "+") + token_of(category) for category in item.categories]
    parts += [f"{DUE_TAG}:{item.due.isoformat()}"] if item.due else []
    parts += [f"{key}:{value}" for key, value in (previous.extras if previous else ())]
    parts.append(f"{ID_TAG}:{task_id}")
    return " ".join(parts)


def add_id(text: str, task_id: str) -> str:
    return f"{text.rstrip()} {ID_TAG}:{task_id}"


FORMAT = LineFormat(KEY, parse_line, render_line, add_id)

__all__ = ["FORMAT", "KEY", "parse_line", "render_line", "add_id", "priority_of_letter", "letter_for"]
