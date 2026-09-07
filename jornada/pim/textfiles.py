"""Text files as the Jornada writes and reads them (Pocket Word .txt, HP Quick Pad, .csv)."""
from __future__ import annotations

import re

DEVICE_TEXT_ENCODING = "cp1252"
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def decode_text(data: bytes) -> str:
    """Bytes from the device → text: BOM-aware, UTF-8 when valid, else Windows-1252."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace").replace("\r\n", "\n")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00":
        return data.decode("utf-16-le", errors="replace").replace("\r\n", "\n")
    try:
        return data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return data.decode(DEVICE_TEXT_ENCODING, errors="replace").replace("\r\n", "\n")


def encode_text(text: str, encoding: str = DEVICE_TEXT_ENCODING) -> bytes:
    """Text → device bytes with CRLF line ends (Pocket Word reads ANSI text)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    return normalized.encode(encoding, errors="replace")


def safe_filename(title: str, extension: str, max_length: int = 60) -> str:
    """A device-safe file name from a title: no path characters, bounded length, never empty."""
    cleaned = _UNSAFE.sub("_", title or "").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "Untitled"
    stem = cleaned[:max_length].rstrip(" .")
    return f"{stem}{extension}"


def unique_filename(name: str, taken: set) -> str:
    """``name`` or ``name-2``, ``name-3`` … until it is not in ``taken`` (case-insensitive)."""
    lowered = {t.lower() for t in taken}
    if name.lower() not in lowered:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    counter = 2
    while True:
        candidate = f"{stem}-{counter}.{ext}" if dot else f"{stem}-{counter}"
        if candidate.lower() not in lowered:
            return candidate
        counter += 1
