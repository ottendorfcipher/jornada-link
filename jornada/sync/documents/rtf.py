"""Rich Text Format the way Pocket Word reads and writes it (RTF 1.5, ANSI code page).

``from_text`` writes a minimal document — one paragraph per line, Tahoma 10pt,
Windows-1252 bytes as ``\\'hh`` and anything beyond as ``\\uN?`` — that Pocket
Word on Windows CE 2.11 opens. ``to_text`` is a tolerant reader: it keeps the
paragraph text of whatever Word, Pocket Word or TextEdit produced and drops the
rest (font, colour and style tables, document info, pictures, field codes and
every unknown control word).
"""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, replace
from typing import Dict, List, Optional

DEVICE_CODE_PAGE = "cp1252"
HEADER = "{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0 Tahoma;}}\\f0\\fs20 "
MAGIC = b"{\\rtf"

_CONTROL_WORD = re.compile(rb"([A-Za-z]{1,32})(-?[0-9]{1,10})? ?")
_SKIPPED_DESTINATIONS = frozenset(
    b"fonttbl colortbl stylesheet info pict object header footer headerl headerr headerf "
    b"footerl footerr footerf footnote fldinst xe tc revtbl listtable listoverridetable "
    b"pntext themedata colorschememapping datastore latentstyles rsidtbl generator "
    b"background docvar template userprops".split()
)
_SYMBOLS: Dict[bytes, str] = {
    b"par": "\n", b"line": "\n", b"row": "\n", b"sect": "\n", b"page": "\n",
    b"tab": "\t", b"cell": "\t",
    b"emdash": "\u2014", b"endash": "\u2013", b"bullet": "\u2022",
    b"lquote": "\u2018", b"rquote": "\u2019", b"ldblquote": "\u201c", b"rdblquote": "\u201d",
    b"emspace": " ", b"enspace": " ", b"qmspace": " ",
    b"zwj": "", b"zwnj": "", b"ltrmark": "", b"rtlmark": "",
}
_SYMBOL_CHARS: Dict[bytes, str] = {b"~": "\u00a0", b"_": "\u2011", b"-": ""}
_CODE_PAGES: Dict[bytes, str] = {b"mac": "mac_roman", b"pc": "cp437", b"pca": "cp850"}


# -- writing --------------------------------------------------------------------
def from_text(text: str) -> bytes:
    """Text → RTF bytes, one paragraph per line; round-trips through :func:`to_text`."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body = "".join(_escape(line) + "\\par\n" for line in lines)
    return (HEADER + body + "}").encode("ascii")


def _escape(line: str) -> str:
    return "".join(_escape_char(char) for char in line)


def _escape_char(char: str) -> str:
    if char == "\t":
        return "\\tab "
    if char in "\\{}":
        return "\\" + char
    code = ord(char)
    if 0x20 <= code < 0x7F:
        return char
    try:
        (byte,) = char.encode(DEVICE_CODE_PAGE)
    except UnicodeEncodeError:
        return _unicode_escape(code)
    return "\\'%02x" % byte


def _unicode_escape(code: int) -> str:
    """``\\uN?`` with N as a signed 16-bit value; astral characters become a surrogate pair."""
    if code > 0xFFFF:
        offset = code - 0x10000
        return _unicode_escape(0xD800 + (offset >> 10)) + _unicode_escape(0xDC00 + (offset & 0x3FF))
    signed = code - 0x10000 if code > 0x7FFF else code
    return f"\\u{signed}?"


# -- reading --------------------------------------------------------------------
def to_text(data: bytes) -> str:
    """RTF bytes → paragraphs separated by ``\\n``; raises ValueError when ``data`` is not RTF."""
    document = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not document.startswith(MAGIC):
        raise ValueError("not an RTF document (no {\\rtf header)")
    return _Reader(document).run()


@dataclass(frozen=True)
class _Group:
    """What a ``{`` … ``}`` group inherits: whether its text is discarded and the ``\\uc`` count."""

    skipping: bool = False
    uc: int = 1


class _Reader:
    """One pass over the bytes. State: the group stack, the code page, the ``\\'hh`` bytes not
    yet decoded (multi-byte code pages need them together) and the fallback characters
    still to drop after a ``\\uN``."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self._groups: List[_Group] = [_Group()]
        self._pieces: List[str] = []
        self._pending = bytearray()
        self._codec = DEVICE_CODE_PAGE
        self._skip_chars = 0

    @property
    def _group(self) -> _Group:
        return self._groups[-1]

    def run(self) -> str:
        data = self._data
        while self._pos < len(data):
            byte = data[self._pos]
            self._pos += 1
            if byte == 0x7B:
                self._open_group()
            elif byte == 0x7D:
                self._close_group()
            elif byte == 0x5C:
                self._control()
            elif byte not in (0x0D, 0x0A):
                self._text_byte(byte)
        return self._finish()

    # -- tokens ---------------------------------------------------------------
    def _control(self) -> None:
        data = self._data
        if self._pos >= len(data):
            return
        match = _CONTROL_WORD.match(data, self._pos)
        if match:
            self._pos = match.end()
            param = int(match.group(2)) if match.group(2) else None
            self._word(match.group(1), param)
            return
        symbol = data[self._pos:self._pos + 1]
        self._pos += 1
        if symbol == b"'":
            self._hex_byte()
        elif symbol == b"*":
            self._skip_group()
        elif symbol in (b"{", b"}", b"\\"):
            self._text_byte(symbol[0])
        elif symbol in (b"\r", b"\n"):
            self._emit("\n")
        elif symbol in _SYMBOL_CHARS:
            self._emit(_SYMBOL_CHARS[symbol])

    def _word(self, word: bytes, param: Optional[int]) -> None:
        if word == b"bin":
            self._pos += max(param or 0, 0)
            return
        self._skip_chars = 0
        if self._group.skipping:
            return
        if word in _SKIPPED_DESTINATIONS:
            self._skip_group()
        elif word == b"uc":
            self._groups[-1] = replace(self._group, uc=max(param or 0, 0))
        elif word == b"u":
            self._unicode(param)
        elif word == b"ansicpg":
            self._set_codec(f"cp{param}" if param else DEVICE_CODE_PAGE)
        elif word in _CODE_PAGES:
            self._set_codec(_CODE_PAGES[word])
        elif word in _SYMBOLS:
            self._emit(_SYMBOLS[word])

    def _hex_byte(self) -> None:
        digits = self._data[self._pos:self._pos + 2]
        try:
            value = int(digits, 16)
        except ValueError:
            return
        self._pos += 2
        if self._group.skipping:
            return
        if self._skip_chars:
            self._skip_chars -= 1
            return
        self._pending.append(value)

    def _unicode(self, param: Optional[int]) -> None:
        if param is None:
            return
        code = param + 0x10000 if param < 0 else param
        if 0 <= code <= 0xFFFF:
            self._emit(chr(code))
        self._skip_chars = self._group.uc

    def _text_byte(self, byte: int) -> None:
        if self._group.skipping:
            return
        if self._skip_chars:
            self._skip_chars -= 1
            return
        if byte >= 0x80:
            self._pending.append(byte)
            return
        self._flush()
        self._pieces.append(chr(byte))

    # -- state ----------------------------------------------------------------
    def _set_codec(self, name: str) -> None:
        self._flush()
        try:
            codecs.lookup(name)
        except LookupError:
            name = DEVICE_CODE_PAGE
        self._codec = name

    def _emit(self, text: str) -> None:
        self._flush()
        self._pieces.append(text)

    def _flush(self) -> None:
        if self._pending:
            self._pieces.append(self._pending.decode(self._codec, errors="replace"))
            self._pending = bytearray()

    def _open_group(self) -> None:
        self._flush()
        self._groups.append(self._group)
        self._skip_chars = 0

    def _close_group(self) -> None:
        self._flush()
        if len(self._groups) > 1:
            self._groups.pop()
        self._skip_chars = 0

    def _skip_group(self) -> None:
        self._groups[-1] = replace(self._group, skipping=True)

    def _finish(self) -> str:
        self._flush()
        text = "".join(self._pieces)
        joined = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
        return joined[:-1] if joined.endswith("\n") else joined
