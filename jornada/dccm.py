"""Minimal "dccm": the desktop end of a Windows CE 2.x ActiveSync connection.

The device, once its PPP link is up, connects to TCP port 5679 on the PPP
peer, announces itself (info packet) and expects the desktop to answer with
the ping word 0x12345678 every few seconds. Only after this exchange does the
device keep the link up and serve RAPI on port 990. Protocol per SynCE dccm.c.
"""
from __future__ import annotations

import logging
import select
import socket
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from . import password as pw
from . import transport, wire
from .constants import (
    DCCM_MAX_MISSED_PINGS,
    DCCM_MAX_PACKET_SIZE,
    DCCM_MIN_PACKET_SIZE,
    DCCM_PING,
    DCCM_PING_INTERVAL_S,
    DCCM_PORT,
)
from .info import DeviceInfo, parse_info_packet
from .state import DEFAULT_STATE_PATH, clear_state, write_state

log = logging.getLogger("jornada.dccm")


class DccmError(RuntimeError):
    """Fatal protocol problem with the connected device."""


@dataclass(frozen=True)
class Session:
    """Immutable snapshot of one device connection's progress."""
    ip: str
    info: Optional[DeviceInfo] = None
    key: int = 0
    locked: bool = False
    awaiting_password_reply: bool = False
    authenticated: bool = False
    missed_pings: int = 0


def handle_header(
    session: Session,
    header: int,
    read_body: Callable[[int], bytes],
    send: Callable[[bytes], None],
    password: Optional[str],
) -> Session:
    """Process one 4-byte header from the device and return the new session.

    ``read_body`` fetches N more bytes, ``send`` writes to the device.
    """
    if header == 0:
        return session  # empty packet, ignore
    if header == DCCM_PING:
        return replace(session, missed_pings=0)
    if header < DCCM_MAX_PACKET_SIZE:
        if header < DCCM_MIN_PACKET_SIZE:
            raise DccmError(f"info packet too small ({header} bytes)")
        info = parse_info_packet(read_body(header))
        log.info("device: name=%r class=%r hardware=%r os=%s build=%d cpu=0x%x",
                 info.name, info.device_class, info.hardware,
                 info.os_version_text, info.build_number, info.processor_type)
        if session.locked:
            return replace(session, info=info, awaiting_password_reply=True)
        send(wire.u32(DCCM_PING))
        return replace(session, info=info, authenticated=True)
    # Anything else is a password challenge; the key is the low byte.
    key = header & 0xFF
    if not password:
        raise DccmError(
            "the device is password-protected; restart with --password"
        )
    encoded = pw.encode_password(password, key)
    send(wire.u16(len(encoded)) + encoded)
    return replace(session, key=key, locked=True)


def handle_password_reply(session: Session, reply: bytes, send: Callable[[bytes], None]) -> Session:
    if int.from_bytes(reply, "little") == 0:
        raise DccmError("device rejected the password")
    send(wire.u32(DCCM_PING))
    return replace(session, awaiting_password_reply=False, authenticated=True)


class DccmServer:
    """Accept device connections and keep them alive with pings."""

    def __init__(
        self,
        bind_ip: str = "0.0.0.0",
        port: int = DCCM_PORT,
        password: Optional[str] = None,
        state_path: Path = DEFAULT_STATE_PATH,
        ping_interval: float = DCCM_PING_INTERVAL_S,
        on_connect: Optional[Callable[[Session], None]] = None,
    ) -> None:
        self._bind_ip = bind_ip
        self._port = port
        self._password = password
        self._state_path = state_path
        self._ping_interval = ping_interval
        self._on_connect = on_connect
        self._listener: Optional[socket.socket] = None
        self._stop = False

    @property
    def port(self) -> int:
        if self._listener is None:
            return self._port
        return self._listener.getsockname()[1]

    def open(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._bind_ip, self._port))
        listener.listen(2)
        self._listener = listener
        log.info("listening on %s:%d for the device", self._bind_ip, self.port)

    def close(self) -> None:
        self._stop = True
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def serve_forever(self) -> None:
        if self._listener is None:
            self.open()
        assert self._listener is not None
        try:
            while not self._stop:
                ready, _, _ = select.select([self._listener], [], [], 0.5)
                if not ready:
                    continue
                conn, addr = self._listener.accept()
                self._serve_client(conn, addr[0])
        finally:
            self.close()

    def serve_one(self, timeout: Optional[float] = None) -> Optional[Session]:
        """Accept a single connection (mainly for tests); returns the final session."""
        if self._listener is None:
            self.open()
        assert self._listener is not None
        ready, _, _ = select.select([self._listener], [], [], timeout)
        if not ready:
            return None
        conn, addr = self._listener.accept()
        return self._serve_client(conn, addr[0])

    def _serve_client(self, conn: socket.socket, ip: str) -> Session:
        log.info("device connected from %s", ip)
        session = Session(ip=ip)
        conn.settimeout(10.0)
        try:
            session = self._client_loop(conn, session)
        except (DccmError, transport.TransportError, wire.WireError, OSError) as exc:
            log.error("connection with %s ended: %s", ip, exc)
        finally:
            conn.close()
            clear_state(self._state_path)
            log.info("device %s disconnected", ip)
        return session

    def _client_loop(self, conn: socket.socket, session: Session) -> Session:
        def send(data: bytes) -> None:
            transport.send_all(conn, data)

        def read_body(size: int) -> bytes:
            return transport.recv_exact(conn, size)

        next_ping = time.monotonic() + self._ping_interval
        while not self._stop and session.missed_pings < DCCM_MAX_MISSED_PINGS:
            wait = max(0.0, next_ping - time.monotonic())
            ready, _, _ = select.select([conn], [], [], wait)
            if ready:
                was_authenticated = session.authenticated
                if session.awaiting_password_reply:
                    session = handle_password_reply(session, transport.recv_exact(conn, 2), send)
                else:
                    header = transport.recv_u32(conn)
                    session = handle_header(session, header, read_body, send, self._password)
                if session.authenticated and not was_authenticated:
                    self._announce(session)
                continue
            send(wire.u32(DCCM_PING))
            session = replace(session, missed_pings=session.missed_pings + 1)
            next_ping = time.monotonic() + self._ping_interval
        if session.missed_pings >= DCCM_MAX_MISSED_PINGS:
            log.warning("device stopped answering pings")
        return session

    def _announce(self, session: Session) -> None:
        info = session.info
        state = {
            "ip": session.ip,
            "connected_at": time.time(),
            "key": session.key,
            "password_required": session.locked,
            "device": info.to_dict() if info else None,
        }
        write_state(self._state_path, state)
        log.info("ActiveSync handshake complete — RAPI available at %s:%d", session.ip, 990)
        if self._on_connect is not None:
            self._on_connect(session)
