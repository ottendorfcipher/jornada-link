"""Apple Notes stores note bodies as HTML; the Jornada wants plain text. Both directions live here.

``html_to_text`` folds the HTML Notes produces (nested ``<div>``s, ``<br>``,
lists, entities) into lines; ``text_to_html`` builds the HTML Notes expects
back from a title and a body. Both are pure functions.
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from typing import Any, List, Tuple

BLOCK_TAGS = frozenset({"div", "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
                        "table", "tr", "hr", "section", "article", "header", "footer"})
LIST_TAGS = frozenset({"ul", "ol"})
SKIPPED_TAGS = frozenset({"style", "script", "head", "title", "object"})
CELL_TAGS = frozenset({"td", "th"})
_NEWLINES = re.compile(r"\s*[\r\n]+\s*")
_SPACE_RUNS = re.compile(r"^ +| {2,}")


class _TextExtractor(HTMLParser):
    """Collects the text lines of a Notes body: blocks and ``<br>`` end lines, entities are decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._lines: List[str] = []
        self._current: List[str] = []
        self._lists: List[Tuple[str, int]] = []
        self._skip_depth = 0

    # -- HTMLParser callbacks ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag == "br":
            self._end_line()
        elif tag in LIST_TAGS:
            self._end_line_if_text()
            self._lists.append((tag, 0))
        elif tag == "li":
            self._start_list_item()
        elif tag in BLOCK_TAGS:
            self._end_line_if_text()
        elif tag in CELL_TAGS and self._current:
            self._current.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in LIST_TAGS:
            self._end_line_if_text()
            self._lists = self._lists[:-1]
        elif tag in BLOCK_TAGS:
            self._end_line_if_text()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.replace("\xa0", " ")
        if not text.strip() and ("\n" in text or "\r" in text):
            return  # the newlines Notes puts between <div>s are not content
        self._current.append(_NEWLINES.sub(" ", text))

    # -- line bookkeeping -------------------------------------------------------
    def _start_list_item(self) -> None:
        self._end_line_if_text()
        if not self._lists:
            return
        kind, count = self._lists[-1]
        self._lists = self._lists[:-1] + [(kind, count + 1)]
        marker = f"{count + 1}. " if kind == "ol" else "- "
        self._current.append("  " * (len(self._lists) - 1) + marker)

    def _end_line(self) -> None:
        self._lines.append("".join(self._current).rstrip())
        self._current = []

    def _end_line_if_text(self) -> None:
        if self._current:
            self._end_line()

    def lines(self) -> Tuple[str, ...]:
        self._end_line_if_text()
        return tuple(self._lines)


def html_to_lines(html: str) -> Tuple[str, ...]:
    """The text lines of a Notes HTML body (block elements and ``<br>`` end a line)."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    return parser.lines()


def html_to_text(html: str, title: str = "") -> str:
    """Plain text of a Notes body; the first line is dropped when it repeats the title."""
    lines = html_to_lines(html)
    if lines and title.strip() and lines[0].strip() == title.strip():
        lines = lines[1:]
    return "\n".join(lines)


def text_to_html(title: str, body: str) -> str:
    """The HTML Notes expects: the title as a heading, then one ``<div>`` per line (``<br>`` when empty)."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n") if body else []
    parts = [f"<div><h1>{escape(title, quote=False)}</h1></div>"]
    parts.extend(f"<div>{line_html(line)}</div>" if line else "<div><br></div>" for line in lines)
    return "\n".join(parts)


def line_html(line: str) -> str:
    """One text line as HTML: escaped, with leading and repeated spaces kept (HTML would collapse them)."""
    escaped = escape(line, quote=False)
    return _SPACE_RUNS.sub(lambda match: "&nbsp;" * len(match.group(0)), escaped)
