"""Encoding/decoding primitives for the RAPI wire format (librapi2-compatible).

Every encoder is a pure function returning ``bytes``; ``Reader`` is a cursor
over an immutable ``bytes`` object (the underlying data is never modified).
"""
from __future__ import annotations

import struct
from typing import Optional

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")


class WireError(ValueError):
    """Raised when a buffer is too short or malformed."""


def u16(value: int) -> bytes:
    return _U16.pack(value & 0xFFFF)


def u32(value: int) -> bytes:
    return _U32.pack(value & 0xFFFFFFFF)


def wstr(text: str) -> bytes:
    """UTF-16LE string with terminating NUL (Windows ``WCHAR`` array)."""
    return text.encode("utf-16-le") + b"\x00\x00"


def string(text: Optional[str]) -> bytes:
    """``rapi_buffer_write_string``: 1, length-in-chars (+NUL), data; or 0 if None."""
    if text is None:
        return u32(0)
    data = wstr(text)
    return u32(1) + u32(len(data) // 2) + data


def optional_string(text: Optional[str]) -> bytes:
    """``rapi_buffer_write_optional_string``: 1, size-in-bytes, 1, data; or 0."""
    if text is None:
        return u32(0)
    data = wstr(text)
    return u32(1) + u32(len(data)) + u32(1) + data


def optional_in(data: Optional[bytes]) -> bytes:
    """``rapi_buffer_write_optional_in``: 1, size, data; or 0 if None."""
    if data is None:
        return u32(0)
    return u32(1) + u32(len(data)) + data


def optional_out(size: Optional[int]) -> bytes:
    """``rapi_buffer_write_optional_out``: 1, size, 0 (caller supplies buffer); or 0."""
    if size is None:
        return u32(0)
    return u32(1) + u32(size) + u32(0)


def frame(payload: bytes) -> bytes:
    """Length-prefixed frame as sent on the RAPI socket."""
    return u32(len(payload)) + payload


class Reader:
    """Sequential reader over a bytes object. Never mutates ``data``."""

    __slots__ = ("_data", "offset")

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self._data = bytes(data)
        self.offset = offset

    @property
    def remaining(self) -> int:
        return len(self._data) - self.offset

    def take(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self._data):
            raise WireError(
                f"need {size} bytes at offset {self.offset}, only {self.remaining} remain"
            )
        chunk = self._data[self.offset:self.offset + size]
        self.offset += size
        return chunk

    def u16(self) -> int:
        return _U16.unpack(self.take(2))[0]

    def u32(self) -> int:
        return _U32.unpack(self.take(4))[0]

    def wchars(self, count: int) -> str:
        """Read ``count`` UTF-16 code units and strip the NUL terminator."""
        raw = self.take(count * 2)
        return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]

    def string(self) -> str:
        """``rapi_buffer_read_string``: length-in-chars, then (length+1) WCHARs."""
        length = self.u32()
        return self.wchars(length + 1)

    def optional(self) -> Optional[bytes]:
        """``rapi_buffer_read_optional``: returns the data, or None if absent."""
        has_parameter = self.u32()
        if has_parameter != 1:
            return None
        size = self.u32()
        has_value = self.u32()
        if has_value != 1:
            return None
        return self.take(size)


def filetime_to_unix(low: int, high: int) -> Optional[float]:
    """Convert a Win32 FILETIME pair to a Unix timestamp (None if zero)."""
    ticks = (high << 32) | low
    if ticks == 0:
        return None
    from .constants import FILETIME_EPOCH_DELTA
    return (ticks - FILETIME_EPOCH_DELTA) / 10_000_000
