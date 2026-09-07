"""Content-line plumbing shared by the iCalendar and vCard codecs.

RFC 5545 (iCalendar), RFC 6350 (vCard 4.0), RFC 2426 (vCard 3.0) and the
vCard 2.1 profile all use the same line syntax::

    [group.]NAME[;PARAM=value[;PARAM="quoted:value"]...]:value

with 75-octet folding (a continuation line starts with one space or tab),
backslash escaping inside text values and double-quoted parameter values.
This module knows nothing about components or cards: it turns text into
logical lines, lines into their parts, and parts back into folded text.

Value model
-----------
Not every property value is free text.  ``RRULE:FREQ=WEEKLY;BYDAY=MO`` must
never have its semicolons escaped, and ``CATEGORIES:A\\,B,C`` or ``N:Doe;John``
lose their structure if they are unescaped as a whole.  :func:`is_text_value`
decides, from the property name and an explicit ``VALUE=`` parameter, whether
a value is single free text (unescaped on parse, escaped on write) or is kept
in wire form (lists, structured values, dates, URIs, recurrence rules).  The
list/structured helpers (:func:`split_unescaped` + :func:`unescape`) then
decode the individual elements losslessly.
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, NamedTuple, Optional, Tuple, Union

Params = Tuple[Tuple[str, str], ...]
ParamsLike = Union[Mapping[str, str], Iterable[Tuple[str, str]], None]

CRLF = "\r\n"
FOLD_LIMIT = 75  # octets per physical line, excluding the CRLF (RFC 5545 §3.1)

# Properties whose values are not a single free-text value: dates, durations,
# recurrence rules, URIs, numbers, comma lists and semicolon-structured values.
# They are stored and written exactly as on the wire.  Everything else (and any
# property carrying ``VALUE=TEXT``) is escaped/unescaped as text.
RAW_VALUE_NAMES = frozenset({
    # iCalendar date/time, duration, recurrence, period, offset, uri, number
    "DTSTART", "DTEND", "DUE", "DTSTAMP", "COMPLETED", "CREATED", "LAST-MODIFIED",
    "RECURRENCE-ID", "EXDATE", "RDATE", "DURATION", "TRIGGER", "RRULE", "EXRULE",
    "FREEBUSY", "TZOFFSETFROM", "TZOFFSETTO", "TZURL", "URL", "ATTACH", "ATTENDEE",
    "ORGANIZER", "GEO", "PRIORITY", "SEQUENCE", "PERCENT-COMPLETE", "REPEAT",
    "ACKNOWLEDGED",
    # iCalendar lists and structured text
    "CATEGORIES", "RESOURCES", "REQUEST-STATUS",
    # vCard structured / list / non-text values
    "N", "ADR", "ORG", "NICKNAME", "TEL", "EMAIL", "PHOTO", "LOGO", "SOUND", "KEY",
    "BDAY", "ANNIVERSARY", "REV", "TZ", "IMPP", "RELATED", "MEMBER", "SOURCE",
    "GENDER", "CLIENTPIDMAP", "LANG", "CALADRURI", "CALURI", "FBURL", "XML",
    "X-ABDATE", "X-ANNIVERSARY", "X-EVOLUTION-ANNIVERSARY",
})

# Parameters whose value is a comma-separated list; their commas are written
# unquoted, while any other parameter value containing a comma is quoted.
MULTI_VALUED_PARAMS = frozenset({
    "TYPE", "MEMBER", "DELEGATED-FROM", "DELEGATED-TO", "PID", "SORT-AS",
})

# vCard 2.1 allows bare parameter words; these mean ENCODING, all others TYPE.
_BARE_ENCODINGS = frozenset({"QUOTED-PRINTABLE", "BASE64", "8BIT", "7BIT", "B"})
_PARAM_QUOTE_TRIGGERS = (";", ":", ",")


class TextPropError(ValueError):
    """A content line could not be parsed."""


class ContentLine(NamedTuple):
    """The four parts of one logical content line; ``value`` is still escaped."""

    group: Optional[str]
    name: str
    params: Params
    value: str


# --- lines -------------------------------------------------------------------

def unfold(text: str) -> Tuple[str, ...]:
    """Split ``text`` into logical lines, joining folded continuations.

    Accepts CRLF, LF or bare CR line ends, drops a leading BOM and skips blank
    lines.  A physical line starting with a space or tab continues the previous
    logical line (the single leading whitespace character is removed).
    """
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for physical in normalized.split("\n"):
        if physical[:1] in (" ", "\t") and lines:
            lines[-1] += physical[1:]
        elif physical.strip():
            lines.append(physical)
    return tuple(lines)


def fold(line: str, limit: int = FOLD_LIMIT) -> str:
    """Fold one logical line into physical lines of at most ``limit`` octets.

    Folding is UTF-8 aware: a multibyte character is never split.  Continuation
    lines start with a single space, which counts toward the limit.
    """
    chunks: List[str] = []
    current: List[str] = []
    budget = limit
    for char in line:
        size = len(char.encode("utf-8"))
        if current and size > budget:
            chunks.append("".join(current))
            current = []
            budget = limit - 1  # the continuation line's leading space
        current.append(char)
        budget -= size
    chunks.append("".join(current))
    return (CRLF + " ").join(chunks)


# --- escaping ----------------------------------------------------------------

def escape(text: str) -> str:
    """Escape a text value: backslash, semicolon, comma and line breaks."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def unescape(text: str) -> str:
    """Undo :func:`escape`; ``\\N`` is accepted for a newline, unknown escapes drop the backslash."""
    if "\\" not in text:
        return text
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            following = text[index + 1]
            out.append("\n" if following in "nN" else following)
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def split_unescaped(value: str, separator: str) -> Tuple[str, ...]:
    """Split ``value`` on every ``separator`` that is not backslash-escaped.

    The returned parts are still escaped; apply :func:`unescape` to each.
    """
    parts: List[str] = []
    current: List[str] = []
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char == "\\" and index + 1 < length:
            current.append(value[index:index + 2])
            index += 2
            continue
        if char == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return tuple(parts)


def is_text_value(name: str, params: ParamsLike = None) -> bool:
    """True when the value of ``name`` is a single free-text value (see module docs)."""
    declared = param_of(normalize_params(params), "VALUE")
    if declared is not None:
        return declared.upper() == "TEXT"
    return name.upper() not in RAW_VALUE_NAMES


def decode_value(name: str, params: ParamsLike, raw: str) -> str:
    """Wire value → stored value (unescaped only for text-typed properties)."""
    return unescape(raw) if is_text_value(name, params) else raw


def encode_value(name: str, params: ParamsLike, value: str) -> str:
    """Stored value → wire value (escaped only for text-typed properties)."""
    return escape(value) if is_text_value(name, params) else value


# --- parameters --------------------------------------------------------------

def normalize_params(params: ParamsLike) -> Params:
    """Coerce a mapping or any iterable of pairs into upper-cased name/value tuples."""
    if params is None:
        return ()
    items = params.items() if isinstance(params, Mapping) else params
    normalized: List[Tuple[str, str]] = []
    for item in items:
        try:
            name, value = item
        except (TypeError, ValueError) as exc:
            raise TextPropError(f"parameter must be a (name, value) pair, got {item!r}") from exc
        normalized.append((str(name).strip().upper(), str(value)))
    return tuple(normalized)


def param_of(params: Params, name: str) -> Optional[str]:
    """First value of parameter ``name`` (case-insensitive), or None."""
    wanted = name.upper()
    for key, value in params:
        if key == wanted:
            return value
    return None


# --- one content line ----------------------------------------------------------

def parse_line(line: str) -> ContentLine:
    """Split one logical line into group, NAME, parameters and the raw value.

    Parameter values may be double-quoted and then contain ``:``, ``;`` and
    ``,``; quotes are removed.  A bare vCard 2.1 parameter (``TEL;WORK:``)
    becomes ``("TYPE", "WORK")`` (``QUOTED-PRINTABLE``/``BASE64`` become
    ``ENCODING``).  Raises :class:`TextPropError` when there is no colon or no
    property name.
    """
    colon = _find_outside_quotes(line, ":")
    if colon < 0:
        raise TextPropError(f"content line has no ':' separator: {line[:60]!r}")
    head, value = line[:colon], line[colon + 1:]
    pieces = _split_outside_quotes(head, ";")
    group, name = _split_group(pieces[0].strip())
    if not name:
        raise TextPropError(f"content line has no property name: {line[:60]!r}")
    params = tuple(pair for piece in pieces[1:] for pair in _parse_param(piece))
    return ContentLine(group, name.upper(), params, value)


def format_line(group: Optional[str], name: str, params: ParamsLike, value: str) -> str:
    """Render one logical line (unfolded) from its parts; ``value`` is already escaped."""
    head = f"{group}.{name.upper()}" if group else name.upper()
    rendered = "".join(
        f";{key}={_format_param_value(key, val)}" for key, val in normalize_params(params)
    )
    return f"{head}{rendered}:{value}"


def _split_group(token: str) -> Tuple[Optional[str], str]:
    group, dot, name = token.rpartition(".")
    if not dot:
        return None, token
    return (group or None), name


def _parse_param(piece: str) -> Tuple[Tuple[str, str], ...]:
    piece = piece.strip()
    if not piece:
        return ()
    key, equals, value = piece.partition("=")
    if not equals:
        bare = key.upper()
        return (("ENCODING", bare) if bare in _BARE_ENCODINGS else ("TYPE", key),)
    return ((key.strip().upper(), _unquote_param_value(value)),)


def _unquote_param_value(value: str) -> str:
    segments = _split_outside_quotes(value, ",")
    return ",".join(_strip_quotes(segment.strip()) for segment in segments)


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _format_param_value(key: str, value: str) -> str:
    clean = value.replace('"', "'").replace("\r", " ").replace("\n", " ")
    if key in MULTI_VALUED_PARAMS:
        return ",".join(_quote_if_needed(part) for part in clean.split(","))
    return _quote_if_needed(clean)


def _quote_if_needed(value: str) -> str:
    if any(trigger in value for trigger in _PARAM_QUOTE_TRIGGERS):
        return f'"{value}"'
    return value


def _find_outside_quotes(text: str, wanted: str) -> int:
    quoted = False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif char == wanted and not quoted:
            return index
    return -1


def _split_outside_quotes(text: str, separator: str) -> List[str]:
    pieces: List[str] = []
    start = 0
    quoted = False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            pieces.append(text[start:index])
            start = index + 1
    pieces.append(text[start:])
    return pieces
