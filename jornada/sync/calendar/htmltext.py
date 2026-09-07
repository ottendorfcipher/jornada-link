"""HTML → plain text for the event bodies Microsoft Graph returns (``contentType: html``).

Outlook wraps even a one-line note in ``<html><head><style>…</style></head><body>``
and writes one ``<p>`` or ``<div>`` per typed line; this gives one text line per
block (never doubling up at block boundaries), keeps an intentionally empty block
as a blank line, turns ``<br>`` into a line break, decodes entities and drops
everything that is not visible text. A pure function over the markup.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, List

BLOCK_TAGS = frozenset({"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table",
                        "ul", "ol", "section", "article", "header", "footer", "hr", "pre"})
SKIPPED_TAGS = frozenset({"style", "script", "head", "title"})   # void tags (meta, link) never get an end tag
CELL_TAGS = frozenset({"td", "th"})
BODY_TAG = "body"
NEWLINE = "\n"
_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")
_BLANK_RUNS = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """Collects visible text; every block starts and ends a line, ``<pre>`` keeps its line breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._marks: List[int] = []
        self._skip_depth = 0
        self._pre_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag == BODY_TAG:
            self._skip_depth = 0                       # visible content by definition, even after an unclosed <head>
        elif tag == "br":
            self._parts.append(NEWLINE)
        elif tag in BLOCK_TAGS:
            self._open_block(tag)
        elif tag in CELL_TAGS:
            self._parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in BLOCK_TAGS:
            self._close_block(tag)

    def _open_block(self, tag: str) -> None:
        self._break()
        self._marks.append(len(self._parts))
        if tag == "pre":
            self._pre_depth += 1

    def _close_block(self, tag: str) -> None:
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
        mark = self._marks.pop() if self._marks else None
        if mark is not None and mark == len(self._parts):
            self._parts.append(NEWLINE)                # an empty block is an intentional blank line
        else:
            self._break()

    def _break(self) -> None:
        """Start a new line unless the text already is at the start of one."""
        if self._parts and not self._parts[-1].endswith(NEWLINE):
            self._parts.append(NEWLINE)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.replace("\xa0", " ")
        if self._pre_depth:
            self._parts.append(text)
            return
        collapsed = _WHITESPACE.sub(" ", text)
        if collapsed.strip() or (self._parts and not self._parts[-1].endswith(NEWLINE)):
            self._parts.append(collapsed)             # whitespace between blocks is layout, not content

    def text(self) -> str:
        lines = "".join(self._parts).split(NEWLINE)
        return _BLANK_RUNS.sub("\n\n", NEWLINE.join(line.strip() for line in lines)).strip()


def html_to_text(html: str) -> str:
    """The visible text of ``html`` with one line per block; entities decoded."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    return parser.text()


__all__ = ["html_to_text"]
