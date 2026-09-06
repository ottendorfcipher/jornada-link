"""Client for the jornada-gpib gateway (``gpibsrv.exe`` on the device).

The gateway speaks the Prologix GPIB-ETHERNET command language over a plain TCP socket, so
this client is a small line-oriented helper: instrument commands are sent as text lines,
controller commands start with ``++``. Responses are read until a line terminator or until
the read timeout expires (the gateway forwards instrument bytes as they arrive).
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Optional

GATEWAY_PORT = 1234
DEFAULT_TIMEOUT_S = 5.0
ESC = b"\x1b"


class GpibError(Exception):
    """Raised when the gateway cannot be reached or gives no answer."""


def escape(payload: bytes) -> bytes:
    """Escape the bytes the Prologix line protocol treats specially (CR, LF, ESC, '+')."""
    out = bytearray()
    for b in payload:
        if b in (0x0A, 0x0D, 0x1B, 0x2B):
            out += ESC
        out.append(b)
    return bytes(out)


@dataclass(frozen=True)
class GatewayAddress:
    host: str
    port: int = GATEWAY_PORT


class GpibGateway:
    """One TCP session with the gateway. Use as a context manager."""

    def __init__(self, address: GatewayAddress, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._address = address
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None

    def __enter__(self) -> "GpibGateway":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((self._address.host, self._address.port), timeout=self._timeout)
        except OSError as exc:
            raise GpibError(
                f"cannot reach the GPIB gateway at {self._address.host}:{self._address.port} ({exc}); "
                "is gpibsrv.exe running on the Jornada and the PPP link up?"
            ) from exc
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _require(self) -> socket.socket:
        if self._sock is None:
            raise GpibError("not connected")
        return self._sock

    def send_line(self, line: str) -> None:
        """Send one line; instrument text is escaped, ``++`` commands are sent verbatim."""
        sock = self._require()
        data = line.encode("ascii", "replace")
        if not line.startswith("++"):
            data = escape(data)
        sock.sendall(data + b"\n")

    def read_line(self, timeout: Optional[float] = None) -> str:
        """Read until a line feed or until nothing more arrives within the timeout."""
        sock = self._require()
        sock.settimeout(timeout if timeout is not None else self._timeout)
        data = bytearray()
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\n"):
                    break
        except socket.timeout:
            if not data:
                raise GpibError("no answer from the instrument (timeout)") from None
        return data.decode("ascii", "replace").rstrip("\r\n")

    def command(self, text: str) -> str:
        """Send a ``++`` controller command and return its one-line answer."""
        self.send_line(text)
        return self.read_line()

    def set_address(self, pad: int, sad: Optional[int] = None) -> None:
        if not 0 <= pad <= 30:
            raise GpibError(f"primary address {pad} outside 0..30")
        line = f"++addr {pad}" if sad is None else f"++addr {pad} {sad + 96}"
        self.send_line(line)

    def write(self, pad: int, text: str) -> None:
        self.set_address(pad)
        self.send_line("++auto 0")
        self.send_line(text)

    def read(self, pad: int, timeout: Optional[float] = None) -> str:
        self.set_address(pad)
        self.send_line("++read eoi")
        return self.read_line(timeout)

    def query(self, pad: int, text: str, timeout: Optional[float] = None) -> str:
        self.write(pad, text)
        self.send_line("++read eoi")
        return self.read_line(timeout)

    def serial_poll(self, pad: int) -> int:
        answer = self.command(f"++spoll {pad}")
        try:
            return int(answer)
        except ValueError:
            raise GpibError(f"unexpected serial poll answer {answer!r}") from None

    def interface_clear(self) -> None:
        self.send_line("++ifc")

    def device_clear(self, pad: int) -> None:
        self.set_address(pad)
        self.send_line("++clr")

    def trigger(self, pad: int) -> None:
        self.set_address(pad)
        self.send_line("++trg")

    def local(self, pad: int) -> None:
        self.set_address(pad)
        self.send_line("++loc")

    def version(self) -> str:
        return self.command("++ver")

    def quit_gateway(self) -> None:
        self.send_line("++quit")
