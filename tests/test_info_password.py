import socket

import pytest

from jornada import password, wire
from jornada.info import DeviceInfo, build_info_packet, parse_info_packet


def test_info_packet_roundtrip():
    info = DeviceInfo(0x0B02, 11171, 0x2A11, 1, 2, "Jornada680", "HPC", "SH3")
    packet = build_info_packet(info)
    assert parse_info_packet(packet) == info
    assert info.os_version_text == "2.11"


def test_info_packet_too_small():
    with pytest.raises(wire.WireError):
        parse_info_packet(b"\x00" * 10)


def test_info_packet_bad_string_offset():
    info = DeviceInfo(3, 1, 1, 0, 0, "X", "Y", "Z")
    packet = bytearray(build_info_packet(info))
    packet[0x18:0x1C] = wire.u32(0xFFFF)
    with pytest.raises(wire.WireError):
        parse_info_packet(bytes(packet))


def test_password_encoding_xors_utf16():
    assert password.encode_password("ab", 0) == b"a\x00b\x00\x00\x00"
    assert password.encode_password("a", 0xFF) == bytes(b ^ 0xFF for b in b"a\x00\x00\x00")
    with pytest.raises(ValueError):
        password.encode_password("a", 300)
    with pytest.raises(TypeError):
        password.encode_password(b"a", 1)  # type: ignore[arg-type]


def test_password_send_and_reply_over_socketpair():
    a, b = socket.socketpair()
    try:
        password.send_password(a, "x", 0x10)
        assert b.recv(2) == wire.u16(4)
        assert b.recv(4) == bytes(c ^ 0x10 for c in b"x\x00\x00\x00")
        b.sendall(b"\x01")
        assert password.recv_password_reply(a, 1) is True
        b.sendall(b"\x00\x00")
        assert password.recv_password_reply(a, 2) is False
        with pytest.raises(ValueError):
            password.recv_password_reply(a, 3)
    finally:
        a.close()
        b.close()
