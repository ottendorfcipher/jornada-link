"""The NOTES blob of appointments, tasks and contacts.

Pocket Outlook stores notes as 8-bit text in the device code page with CRLF
line ends, padded with a trailing 0x03 when the length would be odd; a note may
instead hold Pocket Word ink data, which is kept opaque.
"""
from __future__ import annotations

from typing import Optional

DEVICE_CODEPAGE = "cp1252"
_PAD = b"\x03"
_INK_MAGIC = b"{\\pwi"


def encode_notes(text: str, codepage: str = DEVICE_CODEPAGE) -> bytes:
    """Neutral text (LF line ends) → device blob."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    raw = normalized.encode(codepage, errors="replace")
    if len(raw) % 2:
        raw += _PAD
    return raw


def decode_notes(blob: Optional[bytes], codepage: str = DEVICE_CODEPAGE) -> str:
    """Device blob → neutral text (LF line ends); ink notes decode to ""."""
    if not blob:
        return ""
    if blob.startswith(_INK_MAGIC):
        return ""
    raw = bytes(blob)
    if raw.endswith(_PAD):
        raw = raw[:-1]
    raw = raw.rstrip(b"\x00")
    return raw.decode(codepage, errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def is_ink(blob: Optional[bytes]) -> bool:
    return bool(blob) and bytes(blob).startswith(_INK_MAGIC)
