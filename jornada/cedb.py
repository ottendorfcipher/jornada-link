"""Windows CE object-store database (CEDB) records, librapi2-compatible.

A record travels over RAPI as an array of 16-byte ``CEPROPVAL`` structures
followed by the string and blob payloads they point to. On the wire the
pointer fields are **byte offsets from the start of the buffer** and every
payload starts 4-byte aligned — exactly what librapi2's ``database.c`` writes
and what its pointer fix-up expects when reading.

Everything here is a pure function over immutable values.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from . import wire
from .constants import FILETIME_EPOCH_DELTA

# CEVT_* value types (low word of a CEPROPID)
CEVT_I2 = 2
CEVT_I4 = 3
CEVT_R8 = 5
CEVT_BOOL = 11
CEVT_UI2 = 18
CEVT_UI4 = 19
CEVT_LPWSTR = 31
CEVT_FILETIME = 64
CEVT_BLOB = 65

# CEPROPVAL.wFlags
CEDB_PROPNOTFOUND = 0x0100
CEDB_PROPDELETE = 0x0200

PROPVAL_SIZE = 16
_ALIGNMENT = 4

KIND_NAMES = {
    CEVT_I2: "i2", CEVT_I4: "i4", CEVT_R8: "r8", CEVT_BOOL: "bool",
    CEVT_UI2: "ui2", CEVT_UI4: "ui4", CEVT_LPWSTR: "string",
    CEVT_FILETIME: "filetime", CEVT_BLOB: "blob",
}


class CedbError(ValueError):
    """A record buffer is malformed or a value does not fit its type."""


def align(size: int) -> int:
    """Round ``size`` up to the next multiple of four (librapi2's ``ALIGN``)."""
    return (size + _ALIGNMENT - 1) & ~(_ALIGNMENT - 1)


def propid(prop_id: int, kind: int) -> int:
    """Compose a CEPROPID from the 16-bit property identifier and CEVT type."""
    return ((prop_id & 0xFFFF) << 16) | (kind & 0xFFFF)


@dataclass(frozen=True)
class PropVal:
    """One property of a record: identifier, CEVT type, value, wFlags."""

    prop_id: int
    kind: int
    value: Any
    flags: int = 0

    @property
    def propid(self) -> int:
        return propid(self.prop_id, self.kind)

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, f"0x{self.kind:04x}")

    # -- constructors ---------------------------------------------------------
    @classmethod
    def i2(cls, prop_id: int, value: int) -> "PropVal":
        return cls(prop_id, CEVT_I2, int(value))

    @classmethod
    def i4(cls, prop_id: int, value: int) -> "PropVal":
        return cls(prop_id, CEVT_I4, int(value))

    @classmethod
    def ui2(cls, prop_id: int, value: int) -> "PropVal":
        return cls(prop_id, CEVT_UI2, int(value))

    @classmethod
    def ui4(cls, prop_id: int, value: int) -> "PropVal":
        return cls(prop_id, CEVT_UI4, int(value))

    @classmethod
    def boolean(cls, prop_id: int, value: bool) -> "PropVal":
        return cls(prop_id, CEVT_BOOL, bool(value))

    @classmethod
    def r8(cls, prop_id: int, value: float) -> "PropVal":
        return cls(prop_id, CEVT_R8, float(value))

    @classmethod
    def filetime(cls, prop_id: int, ticks: int) -> "PropVal":
        return cls(prop_id, CEVT_FILETIME, int(ticks))

    @classmethod
    def string(cls, prop_id: int, value: str) -> "PropVal":
        return cls(prop_id, CEVT_LPWSTR, str(value))

    @classmethod
    def blob(cls, prop_id: int, value: bytes) -> "PropVal":
        return cls(prop_id, CEVT_BLOB, bytes(value))

    @classmethod
    def deleted(cls, prop_id: int, kind: int) -> "PropVal":
        """A property marked for removal on the next write (CEDB_PROPDELETE)."""
        empty: Any = "" if kind == CEVT_LPWSTR else b"" if kind == CEVT_BLOB else 0
        return cls(prop_id, kind, empty, CEDB_PROPDELETE)

    def to_json(self) -> Dict[str, Any]:
        value: Any = self.value
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).hex()   # blobs, and the raw union bytes of an unknown type
        return {"id": self.prop_id, "kind": self.kind_name, "value": value, "flags": self.flags}


@dataclass(frozen=True)
class Record:
    """A database record: its object identifier and properties."""

    oid: int
    props: Tuple[PropVal, ...]

    def get(self, prop_id: int) -> Optional[PropVal]:
        for prop in self.props:
            if prop.prop_id == prop_id:
                return prop
        return None

    def value(self, prop_id: int, default: Any = None) -> Any:
        prop = self.get(prop_id)
        return default if prop is None else prop.value

    def to_json(self) -> Dict[str, Any]:
        return {"oid": self.oid, "props": [p.to_json() for p in self.props]}


# -- FILETIME helpers ---------------------------------------------------------
def filetime_to_unix(ticks: int) -> Optional[float]:
    """100 ns ticks since 1601 → Unix seconds (None for zero)."""
    if ticks == 0:
        return None
    return (ticks - FILETIME_EPOCH_DELTA) / 10_000_000


def unix_to_filetime(seconds: float) -> int:
    return int(round(seconds * 10_000_000)) + FILETIME_EPOCH_DELTA


# -- packing ------------------------------------------------------------------
def _payload_size(prop: PropVal) -> int:
    if prop.kind == CEVT_LPWSTR:
        return len(wire.wstr(prop.value))
    if prop.kind == CEVT_BLOB:
        return len(prop.value)
    return 0


def _pack_value(prop: PropVal, payload_offset: int) -> bytes:
    """The 8-byte CEVALUNION as librapi2's PreparePropValForWriting emits it."""
    kind, value = prop.kind, prop.value
    if kind == CEVT_LPWSTR:
        return wire.u32(payload_offset) + wire.u32(0)
    if kind == CEVT_BLOB:
        return wire.u32(len(value)) + wire.u32(payload_offset)
    if kind == CEVT_I2:
        return struct.pack("<i", _check_range(prop, -0x8000, 0x7FFF)) + wire.u32(0)
    if kind == CEVT_I4:
        return struct.pack("<i", _check_range(prop, -0x80000000, 0x7FFFFFFF)) + wire.u32(0)
    if kind == CEVT_UI2:
        return struct.pack("<I", _check_range(prop, 0, 0xFFFF)) + wire.u32(0)
    if kind == CEVT_UI4:
        return struct.pack("<I", _check_range(prop, 0, 0xFFFFFFFF)) + wire.u32(0)
    if kind == CEVT_BOOL:
        return wire.u32(1 if value else 0) + wire.u32(0)
    if kind == CEVT_FILETIME:
        ticks = _check_range(prop, 0, 0xFFFFFFFFFFFFFFFF)
        return wire.u32(ticks & 0xFFFFFFFF) + wire.u32(ticks >> 32)
    if kind == CEVT_R8:
        return struct.pack("<d", float(value))
    raise CedbError(f"cannot pack property 0x{prop.prop_id:04x} of type 0x{kind:04x}")


def _check_range(prop: PropVal, low: int, high: int) -> int:
    value = int(prop.value)
    if not low <= value <= high:
        raise CedbError(f"property 0x{prop.prop_id:04x} value {value} out of range for {prop.kind_name}")
    return value


def pack_record(props: Tuple[PropVal, ...]) -> bytes:
    """Serialize properties into a CeWriteRecordProps buffer."""
    header = bytearray()
    payloads = bytearray()
    offset = align(len(props) * PROPVAL_SIZE)
    for prop in props:
        size = _payload_size(prop)
        header += wire.u32(prop.propid) + wire.u16(0) + wire.u16(prop.flags)
        header += _pack_value(prop, offset if size else 0)
        if prop.kind == CEVT_LPWSTR:
            payloads += wire.wstr(prop.value)
        elif prop.kind == CEVT_BLOB:
            payloads += bytes(prop.value)
        padded = align(offset + size)
        payloads += bytes(padded - offset - size)
        offset = padded
    return bytes(header) + bytes(payloads)


# -- unpacking ----------------------------------------------------------------
def _slice(data: bytes, offset: int, size: int, what: str) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise CedbError(f"{what} at offset {offset} (+{size}) is outside the {len(data)}-byte record")
    return data[offset:offset + size]


def _read_wstr_at(data: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        raise CedbError(f"string offset {offset} is outside the {len(data)}-byte record")
    raw = data[offset:]
    end = 0
    while end + 1 < len(raw) and raw[end:end + 2] != b"\x00\x00":
        end += 2
    return raw[:end].decode("utf-16-le", errors="replace")


def _unpack_value(kind: int, val: bytes, data: bytes, prop_id: int) -> Any:
    if kind == CEVT_LPWSTR:
        return _read_wstr_at(data, struct.unpack("<I", val[:4])[0])
    if kind == CEVT_BLOB:
        count, offset = struct.unpack("<II", val)
        return _slice(data, offset, count, f"blob 0x{prop_id:04x}")
    if kind == CEVT_I2:
        return struct.unpack("<h", val[:2])[0]
    if kind == CEVT_UI2:
        return struct.unpack("<H", val[:2])[0]
    if kind == CEVT_I4:
        return struct.unpack("<i", val[:4])[0]
    if kind in (CEVT_UI4, CEVT_BOOL):
        value = struct.unpack("<I", val[:4])[0]
        return bool(value) if kind == CEVT_BOOL else value
    if kind == CEVT_FILETIME:
        low, high = struct.unpack("<II", val)
        return (high << 32) | low
    if kind == CEVT_R8:
        return struct.unpack("<d", val)[0]
    return bytes(val)  # unknown type: keep the raw union bytes


def unpack_record(data: bytes, count: int) -> Tuple[PropVal, ...]:
    """Parse a CeReadRecordProps buffer holding ``count`` properties."""
    if count * PROPVAL_SIZE > len(data):
        raise CedbError(f"{count} properties need {count * PROPVAL_SIZE} bytes, record has {len(data)}")
    props = []
    for index in range(count):
        entry = data[index * PROPVAL_SIZE:(index + 1) * PROPVAL_SIZE]
        cepropid, _len_data, flags = struct.unpack("<IHH", entry[:8])
        prop_id, kind = cepropid >> 16, cepropid & 0xFFFF
        if flags & CEDB_PROPNOTFOUND:
            props.append(PropVal(prop_id, kind, None, flags))
            continue
        props.append(PropVal(prop_id, kind, _unpack_value(kind, entry[8:16], data, prop_id), flags))
    return tuple(props)
