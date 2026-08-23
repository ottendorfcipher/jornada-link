"""Non-root serial-line probe for a Windows CE device behind an FTDI adapter.

Opens the port raw at a given baud, reports modem-control line state
(CTS/DSR/DCD tell us whether a powered device is on the other end), and
logs whatever bytes arrive for a few seconds. When the Jornada starts
"PC Link" / a direct connection it sends the ASCII string ``CLIENT``.

Only the standard library is used (termios/fcntl), so this runs without
pyserial or sudo.
"""
from __future__ import annotations

import fcntl
import os
import select
import sys
import termios
import time
from dataclasses import dataclass
from typing import Optional

# macOS ioctl numbers (from <sys/ttycom.h>)
TIOCMGET = 0x4004746A
TIOCM_DTR = 0x002
TIOCM_RTS = 0x004
TIOCM_CTS = 0x020
TIOCM_CAR = 0x040  # DCD
TIOCM_DSR = 0x100

BAUD_CONSTANTS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


@dataclass(frozen=True)
class ModemLines:
    dtr: bool
    rts: bool
    cts: bool
    dsr: bool
    dcd: bool

    @classmethod
    def from_bits(cls, bits: int) -> "ModemLines":
        return cls(
            dtr=bool(bits & TIOCM_DTR),
            rts=bool(bits & TIOCM_RTS),
            cts=bool(bits & TIOCM_CTS),
            dsr=bool(bits & TIOCM_DSR),
            dcd=bool(bits & TIOCM_CAR),
        )

    def describe(self) -> str:
        flags = [
            f"DTR={'1' if self.dtr else '0'}",
            f"RTS={'1' if self.rts else '0'}",
            f"CTS={'1' if self.cts else '0'}",
            f"DSR={'1' if self.dsr else '0'}",
            f"DCD={'1' if self.dcd else '0'}",
        ]
        return " ".join(flags)


def open_raw(device: str, baud: int) -> int:
    """Open ``device`` in raw 8N1 mode at ``baud`` and return the fd."""
    if baud not in BAUD_CONSTANTS:
        raise ValueError(f"unsupported baud {baud}; choose from {sorted(BAUD_CONSTANTS)}")
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    cflag = (cflag & ~termios.CSIZE) | termios.CS8
    cflag &= ~(termios.PARENB | termios.CSTOPB)
    cflag |= termios.CLOCAL | termios.CREAD
    cflag &= ~getattr(termios, "CRTSCTS", 0)
    iflag = 0
    oflag = 0
    lflag = 0
    speed = BAUD_CONSTANTS[baud]
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_modem_lines(fd: int) -> Optional[ModemLines]:
    """Return the modem-control line state, or None if the tty has none (e.g. a pty)."""
    buf = bytearray(4)
    try:
        fcntl.ioctl(fd, TIOCMGET, buf)
    except OSError:
        return None
    return ModemLines.from_bits(int.from_bytes(buf, sys.byteorder))


def _printable(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def sniff(device: str, baud: int, seconds: float, out=None) -> bytes:
    """Log bytes seen on ``device`` for ``seconds``; return everything captured."""
    out = out if out is not None else sys.stdout
    fd = open_raw(device, baud)
    captured = b""
    try:
        lines = read_modem_lines(fd)
        described = lines.describe() if lines else "n/a"
        out.write(f"[{device} @ {baud}] modem lines: {described}\n")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.25)
            if not ready:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                continue
            captured += chunk
            out.write(f"  rx {len(chunk):4d} B  hex={chunk[:32].hex(' ')}  ascii={_printable(chunk[:48])!r}\n")
            out.flush()
        if not captured:
            out.write(f"  (no bytes received in {seconds:g}s)\n")
        if b"CLIENT" in captured:
            out.write("  >>> saw 'CLIENT' — the device is requesting a Windows direct-cable PPP session\n")
    finally:
        os.close(fd)
    return captured


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: serial_probe.py /dev/cu.usbserial-XXXX [baud=115200] [seconds=8]\n")
        return 2
    device = argv[1]
    baud = int(argv[2]) if len(argv) > 2 else 115200
    seconds = float(argv[3]) if len(argv) > 3 else 8.0
    try:
        sniff(device, baud, seconds)
    except OSError as exc:
        sys.stderr.write(f"error: cannot use {device}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
