"""Device password exchange (libsynce password.c)."""
from __future__ import annotations

import socket

from . import transport, wire


def encode_password(password: str, key: int) -> bytes:
    """UTF-16LE password + NUL, each byte XOR-ed with the 8-bit key."""
    if not isinstance(password, str):
        raise TypeError("password must be a str")
    if not 0 <= key <= 0xFF:
        raise ValueError(f"key must be a byte value, got {key!r}")
    return bytes(b ^ key for b in wire.wstr(password))


def send_password(sock: socket.socket, password: str, key: int) -> None:
    """Send ``u16 size`` followed by the encoded password."""
    encoded = encode_password(password, key)
    transport.send_all(sock, wire.u16(len(encoded)) + encoded)


def recv_password_reply(sock: socket.socket, size: int) -> bool:
    """Read a 1- or 2-byte reply; non-zero means the password was accepted."""
    if size not in (1, 2):
        raise ValueError("reply size must be 1 or 2")
    raw = transport.recv_exact(sock, size)
    return int.from_bytes(raw, "little") != 0
