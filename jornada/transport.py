"""Blocking socket helpers shared by the dccm listener and the RAPI client."""
from __future__ import annotations

import socket
from typing import Optional

from . import wire


class TransportError(ConnectionError):
    """Socket closed or timed out mid-message."""


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly ``size`` bytes or raise ``TransportError``."""
    chunks = []
    remaining = size
    while remaining > 0:
        try:
            chunk = sock.recv(remaining)
        except socket.timeout as exc:
            raise TransportError(f"timed out waiting for {remaining} of {size} bytes") from exc
        if not chunk:
            raise TransportError(f"connection closed with {remaining} of {size} bytes unread")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_all(sock: socket.socket, data: bytes) -> None:
    try:
        sock.sendall(data)
    except OSError as exc:
        raise TransportError(f"send failed: {exc}") from exc


def send_frame(sock: socket.socket, payload: bytes) -> None:
    send_all(sock, wire.frame(payload))


def recv_frame(sock: socket.socket, max_size: Optional[int] = None) -> bytes:
    size = wire.Reader(recv_exact(sock, 4)).u32()
    if max_size is not None and size > max_size:
        raise TransportError(f"frame of {size} bytes exceeds limit {max_size}")
    return recv_exact(sock, size)


def recv_u32(sock: socket.socket) -> int:
    return wire.Reader(recv_exact(sock, 4)).u32()


def send_u32(sock: socket.socket, value: int) -> None:
    send_all(sock, wire.u32(value))
