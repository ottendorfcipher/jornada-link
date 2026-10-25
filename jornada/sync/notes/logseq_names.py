"""Logseq's file naming (the ``:file/name-format :triple-lowbar`` convention) and page tags.

A page ``a/b: c?`` lives in ``pages/a___b%3A c%3F.md``: the namespace slash
becomes ``___`` and the characters a file name cannot carry are percent-encoded
(``%`` itself first, so the mapping is round-trippable). Journals are
``journals/YYYY_MM_DD.md`` and carry the title ``YYYY-MM-DD``.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional, Tuple
from urllib.parse import unquote

PAGES = "pages"
JOURNALS = "journals"
KINDS = (PAGES, JOURNALS)
NAMESPACE_SEPARATOR = "___"
ENCODED_CHARACTERS = '%:*?"<>|#\\'
_EXTENSION = ".md"
_JOURNAL_FILE = re.compile(r"^(\d{4})_(\d{2})_(\d{2})$")
_JOURNAL_TITLE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TAGS_PROPERTY = re.compile(r"^[ \t]*tags::[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _stem(filename: str) -> str:
    return filename[:-len(_EXTENSION)] if filename.lower().endswith(_EXTENSION) else filename


def _encode_char(char: str) -> str:
    return f"%{ord(char):02X}" if char in ENCODED_CHARACTERS else char


# -- pages ------------------------------------------------------------------------
def page_name_to_filename(name: str) -> str:
    """``"a/b: c?"`` → ``"a___b%3A c%3F.md"``; refuses an empty name."""
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("a Logseq page needs a title")
    encoded = "".join(_encode_char(char) for char in cleaned)
    if encoded.startswith("."):
        encoded = "%2E" + encoded[1:]   # never a hidden dot-file (they would vanish from listings)
    return encoded.replace("/", NAMESPACE_SEPARATOR) + _EXTENSION


def filename_to_page_name(filename: str) -> str:
    """The inverse of :func:`page_name_to_filename` (``.md`` optional)."""
    return unquote(_stem(filename).replace(NAMESPACE_SEPARATOR, "/"))


# -- journals ---------------------------------------------------------------------
def journal_title_to_filename(title: str) -> str:
    """``"2026-09-06"`` → ``"2026_09_06.md"``; anything but a real date is refused."""
    match = _JOURNAL_TITLE.match(title.strip())
    if not match:
        raise ValueError(f"journal titles must be YYYY-MM-DD dates, not {title!r}")
    year, month, day = match.groups()
    try:
        date(int(year), int(month), int(day))
    except ValueError as exc:
        raise ValueError(f"{title!r} is not a calendar date") from exc
    return f"{year}_{month}_{day}{_EXTENSION}"


def filename_to_journal_title(filename: str) -> Optional[str]:
    """``"2026_09_06.md"`` → ``"2026-09-06"``; None for a file that is not a journal."""
    match = _JOURNAL_FILE.match(_stem(filename))
    if not match:
        return None
    return "-".join(match.groups())


# -- tags -------------------------------------------------------------------------
def _clean_tag(tag: str) -> str:
    return tag.strip().strip("[]").lstrip("#").strip().casefold()


def _property_tags(text: str) -> Tuple[str, ...]:
    values = []
    for match in _TAGS_PROPERTY.finditer(text):
        values.extend(_clean_tag(part) for part in match.group(1).split(",") if _clean_tag(part))
    return tuple(values)


def has_tag(text: str, tag: str) -> bool:
    """True when the page carries ``#tag`` / ``#[[tag]]`` or lists it in a ``tags::`` property."""
    wanted = _clean_tag(tag)
    if not wanted:
        return True
    inline = re.compile(r"(?<![\w#])#(?:\[\[)?" + re.escape(wanted) + r"(?:\]\])?(?![\w/-])", re.IGNORECASE)
    return bool(inline.search(text)) or wanted in _property_tags(text)


def with_tag(text: str, tag: str) -> str:
    """The page text carrying ``tag``: unchanged when it already does, else via a ``tags::`` property."""
    wanted = tag.strip().lstrip("#").strip()
    if not wanted or has_tag(text, wanted):
        return text
    match = _TAGS_PROPERTY.search(text)
    if match is None:
        return f"tags:: {wanted}\n{text}"
    existing = match.group(1).strip()
    line = f"tags:: {existing}, {wanted}" if existing else f"tags:: {wanted}"
    return text[:match.start()] + line + text[match.end():]
