"""A fake jornada-gpib gateway (Prologix dialect) with one simulated instrument at address 1."""
from __future__ import annotations

import socket
import threading
from typing import List


class FakeGateway:
    def __init__(self) -> None:
        self.received: List[str] = []
        self.addr = 1
        self.auto = 0
        self.pending: List[str] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.stopped = threading.Event()

    def start(self) -> "FakeGateway":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopped.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _instrument(self, text: str) -> None:
        if self.addr != 1:
            return
        if text == "*IDN?":
            self.pending.append("FAKE,TDS 340,0,FV:v1.02\n")
        elif text == "*ESR?":
            self.pending.append("128\n")
        elif text.endswith("?"):
            self.pending.append("0\n")

    def _handle(self, line: str, conn: socket.socket) -> None:
        self.received.append(line)
        if line.startswith("++"):
            words = line[2:].split()
            cmd = words[0] if words else ""
            if cmd == "ver":
                conn.sendall(b"fake gateway 1.0\n")
            elif cmd == "addr":
                if len(words) > 1:
                    self.addr = int(words[1])
                else:
                    conn.sendall(f"{self.addr}\n".encode())
            elif cmd == "auto":
                if len(words) > 1:
                    self.auto = int(words[1])
            elif cmd == "read":
                if self.pending:
                    conn.sendall(self.pending.pop(0).encode())
            elif cmd == "spoll":
                conn.sendall(b"64\n" if self.addr == 1 else b"")
            elif cmd == "quit":
                self.stopped.set()
        else:
            self._instrument(line)
            if self.auto and line.endswith("?") and self.pending:
                conn.sendall(self.pending.pop(0).encode())

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while not self.stopped.is_set():
                    try:
                        chunk = conn.recv(1024)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        self._handle(raw.decode().replace("\x1b", ""), conn)
