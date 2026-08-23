"""Parsing of the dccm "information" packet a CE device sends on connect."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

from . import wire
from .constants import DCCM_MIN_PACKET_SIZE


@dataclass(frozen=True)
class DeviceInfo:
    os_version: int
    build_number: int
    processor_type: int
    partner_id_1: int
    partner_id_2: int
    name: str
    device_class: str
    hardware: str

    @property
    def os_version_text(self) -> str:
        """Low byte is the major version, high byte the minor (0x0B02 -> "2.11")."""
        return f"{self.os_version & 0xFF}.{self.os_version >> 8}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _string_at(buffer: bytes, offset: int) -> str:
    pointer = wire.Reader(buffer, offset).u32()
    if pointer >= len(buffer):
        raise wire.WireError(f"string offset 0x{pointer:x} outside {len(buffer)}-byte packet")
    raw = buffer[pointer:]
    return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


def parse_info_packet(buffer: bytes) -> DeviceInfo:
    """Decode the packet body (after the 4-byte length header)."""
    if len(buffer) < DCCM_MIN_PACKET_SIZE:
        raise wire.WireError(
            f"info packet is {len(buffer)} bytes, minimum is {DCCM_MIN_PACKET_SIZE}"
        )
    reader = wire.Reader(buffer, 4)
    os_version = reader.u16()
    build_number = reader.u16()
    processor_type = reader.u16()
    reader = wire.Reader(buffer, 0x10)
    partner_id_1 = reader.u32()
    partner_id_2 = reader.u32()
    return DeviceInfo(
        os_version=os_version,
        build_number=build_number,
        processor_type=processor_type,
        partner_id_1=partner_id_1,
        partner_id_2=partner_id_2,
        name=_string_at(buffer, 0x18),
        device_class=_string_at(buffer, 0x1C),
        hardware=_string_at(buffer, 0x20),
    )


def build_info_packet(info: DeviceInfo) -> bytes:
    """Inverse of ``parse_info_packet`` (used by tests and the fake device)."""
    header_size = 0x24
    strings = [info.name, info.device_class, info.hardware]
    encoded = [wire.wstr(s) for s in strings]
    offsets = []
    cursor = header_size
    for data in encoded:
        offsets.append(cursor)
        cursor += len(data)
    body = (
        wire.u32(header_size)
        + wire.u16(info.os_version)
        + wire.u16(info.build_number)
        + wire.u16(info.processor_type)
        + wire.u16(0)
        + wire.u32(0)
        + wire.u32(info.partner_id_1)
        + wire.u32(info.partner_id_2)
        + b"".join(wire.u32(o) for o in offsets)
        + b"".join(encoded)
    )
    return body
