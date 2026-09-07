"""Markdown checklists with the Obsidian Tasks conventions.

``- [ ] Buy milk #errands ⏫ 🛫 2026-09-01 📅 2026-09-10 🆔 k7d2`` /
``- [x] Buy milk ✅ 2026-09-08``: ``📅`` due, ``🛫`` start, ``✅`` completion,
``⏫``/``🔺`` high, ``🔽``/``⏬`` low, ``🆔`` id, ``#tag`` → category. Other
Tasks fields (``⏳`` scheduled, ``➕`` created, ``🔁`` recurrence, ``❌``, ``🏁``)
are kept verbatim. Only ``- [ ]`` / ``- [x]`` items (any marker, any indent)
are tasks; every other line is left untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional, Tuple

from ...pim.models import Task
from .common import NO_SUBJECT
from .linetask import UNKNOWN_COMPLETION_DATE, LineFormat, ParsedTask, completion_text, parse_date, token_of

KEY = "markdown"
DUE, START, DONE, ID = "📅", "🛫", "✅", "🆔"
PRIORITY_EMOJIS = {"🔺": "high", "⏫": "high", "🔼": "normal", "🔽": "low", "⏬": "low"}
PRIORITY_MARKS = {"high": "⏫", "low": "🔽"}
KEPT_EMOJIS = ("⏳", "➕", "🔁", "❌", "🏁")
_TASK = re.compile(r"^(?P<indent>[ \t]*)(?P<bullet>[-*+]|\d+[.)])[ \t]+\[(?P<mark>[ xX])\][ \t]*(?P<rest>.*)$")
_FIELD = re.compile("(" + "|".join(re.escape(e) for e in (DUE, START, DONE, ID, *PRIORITY_EMOJIS, *KEPT_EMOJIS)) + ")")
_TAG = re.compile(r"(?<!\S)#([\w/-]+)")


@dataclass(frozen=True)
class _Fields:
    """The Tasks fields read off one line so far, plus the text a rewrite must keep."""

    due: Optional[date] = None
    start: Optional[date] = None
    completed: Optional[date] = None
    explicit_id: Optional[str] = None
    priority: str = "normal"
    tail: Tuple[str, ...] = ()

    def keeping(self, text: str) -> "_Fields":
        return replace(self, tail=self.tail + (text,)) if text else self


_DATE_FIELDS = {DUE: "due", START: "start", DONE: "completed"}


def parse_line(text: str) -> Optional[ParsedTask]:
    """One line → ParsedTask, or None when it is not a checklist item."""
    match = _TASK.match(text.rstrip("\r\n"))
    if match is None:
        return None
    pieces = _FIELD.split(match.group("rest"))
    fields = _read_fields(tuple(zip(pieces[1::2], pieces[2::2])))
    done = match.group("mark") in "xX"
    description = pieces[0]
    tags = tuple(_TAG.findall(description))
    task = Task(
        summary=" ".join(_TAG.sub("", description).split()),
        due=fields.due, start=fields.start,
        completed=(fields.completed or UNKNOWN_COMPLETION_DATE) if done else None,
        priority=fields.priority, categories=tags,
    )
    return ParsedTask(task, explicit_id=fields.explicit_id, indent=match.group("indent"), bullet=match.group("bullet"),
                      tail=fields.tail)


def _read_fields(pairs: Tuple[Tuple[str, str], ...]) -> _Fields:
    fields = _Fields()
    for emoji, raw in pairs:
        fields = _with_field(fields, emoji, raw)
    return fields


def _with_field(fields: _Fields, emoji: str, raw: str) -> _Fields:
    """``fields`` plus the ``emoji`` field whose text is ``raw``; text that is not its value is kept."""
    value, remainder = _first_token(raw)
    if emoji in _DATE_FIELDS:
        day = parse_date(value)
        dated = replace(fields, **{_DATE_FIELDS[emoji]: day})
        return dated.keeping(raw.strip() if day is None else remainder)
    if emoji == ID:
        return replace(fields, explicit_id=value or None).keeping(remainder)
    if emoji in PRIORITY_EMOJIS:
        return replace(fields, priority=PRIORITY_EMOJIS[emoji]).keeping(raw.strip())
    return fields.keeping(f"{emoji} {raw.strip()}".strip())


def _first_token(raw: str) -> Tuple[str, str]:
    parts = raw.strip().split(None, 1)
    return (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("", "")


def render_line(task: Task, task_id: str, previous: Optional[ParsedTask], today: date) -> str:
    """Task → one checklist line carrying ``🆔 task_id``; ``previous`` supplies indent, marker and kept fields."""
    item = task.normalized()
    indent = previous.indent if previous else ""
    bullet = previous.bullet if previous else "-"
    mark = "x" if item.is_completed else " "
    parts = [f"{indent}{bullet} [{mark}] {' '.join((item.summary or NO_SUBJECT).split())}"]
    parts += [f"#{token_of(category)}" for category in item.categories]
    parts += [PRIORITY_MARKS[item.priority]] if item.priority in PRIORITY_MARKS else []
    parts += list(previous.tail) if previous else []
    parts += [f"{START} {item.start.isoformat()}"] if item.start else []
    parts += [f"{DUE} {item.due.isoformat()}"] if item.due else []
    parts += [f"{DONE} {completion_text(item.completed, today)}"] if item.is_completed else []
    parts.append(f"{ID} {task_id}")
    return " ".join(parts)


def add_id(text: str, task_id: str) -> str:
    return f"{text.rstrip()} {ID} {task_id}"


FORMAT = LineFormat(KEY, parse_line, render_line, add_id)

__all__ = ["FORMAT", "KEY", "parse_line", "render_line", "add_id", "PRIORITY_EMOJIS", "KEPT_EMOJIS"]
