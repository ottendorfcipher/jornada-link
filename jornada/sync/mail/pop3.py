"""A small POP3 server (RFC 1939 plus CAPA) that fronts a mail backend for the device's Inbox.

One backend session per connection: the mailbox is listed at login, messages are
fetched lazily and cached for the connection, DELE only marks, and QUIT commits
the marks through the backend. The password the device sends is compared and
forgotten; it is never logged or echoed.
"""
from __future__ import annotations

import socketserver
from dataclasses import dataclass, field, replace
from typing import Callable, FrozenSet, Mapping, Optional, Tuple

from .backend import MailBackend, MailError, MailSummary
from .config import BridgeConfig
from .messages import CRLF, build_stub, multiline_reply, normalize_crlf, pop3_uid, split_headers, top_lines, dot_stuff, ensure_final_crlf

Log = Callable[[str], None]
MAX_LINE = 255
GREETING = b"+OK Jornada mail bridge ready"
CAPABILITIES = (b"TOP", b"USER", b"UIDL", b"RESP-CODES", b"IMPLEMENTATION Jornada mail bridge")
COMMANDS = {"USER": "cmd_user", "PASS": "cmd_pass", "QUIT": "cmd_quit", "STAT": "cmd_stat", "LIST": "cmd_list",
            "UIDL": "cmd_uidl", "RETR": "cmd_retr", "TOP": "cmd_top", "DELE": "cmd_dele", "RSET": "cmd_rset",
            "NOOP": "cmd_noop", "CAPA": "cmd_capa", "LAST": "cmd_last"}
OPEN_COMMANDS = frozenset({"USER", "PASS", "QUIT", "CAPA", "NOOP"})


def silent(_line: str) -> None:
    return


def safe_text(exc: BaseException) -> bytes:
    """One short ASCII line for a protocol reply (no CR/LF, no secrets — backends never include any)."""
    return " ".join(str(exc).split())[:200].encode("ascii", "replace")


@dataclass(frozen=True)
class Entry:
    """A message as this session numbers it; ``stub`` replaces messages above max_size."""

    number: int
    uid: str
    size: int
    stub: Optional[bytes] = None

    @property
    def uidl(self) -> str:
        return pop3_uid(self.uid)


@dataclass(frozen=True)
class Session:
    user: str = ""
    attempts: int = 0
    authenticated: bool = False
    entries: Tuple[Entry, ...] = ()
    deleted: FrozenSet[int] = frozenset()
    bodies: Mapping[str, bytes] = field(default_factory=dict)
    headers: Mapping[str, bytes] = field(default_factory=dict)
    last: int = 0

    def kept(self) -> Tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.number not in self.deleted)

    def deleted_uids(self) -> Tuple[str, ...]:
        return tuple(entry.uid for entry in self.entries if entry.number in self.deleted)


def load_entries(backend: MailBackend, config: BridgeConfig) -> Tuple[Entry, ...]:
    return tuple(_entry(backend, config, number, summary) for number, summary in enumerate(backend.list(), 1))


def _entry(backend: MailBackend, config: BridgeConfig, number: int, summary: MailSummary) -> Entry:
    if summary.size <= config.max_size:
        return Entry(number, summary.uid, summary.size)
    try:
        headers = backend.fetch_headers(summary.uid)
    except MailError:
        headers = b""
    stub = build_stub(headers, summary.size, config.max_size)
    return Entry(number, summary.uid, len(stub), stub)


class Pop3Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: Tuple[str, int], config: BridgeConfig, backend: MailBackend,
                 log: Log = silent) -> None:
        self.config = config
        self.backend = backend
        self.log = log
        super().__init__(address, Pop3Handler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])


class Pop3Handler(socketserver.StreamRequestHandler):
    server: Pop3Server
    disable_nagle_algorithm = True

    def setup(self) -> None:
        self.timeout = self.server.config.session_timeout
        super().setup()
        self.session = Session()
        self.peer = f"{self.client_address[0]}:{self.client_address[1]}"

    def handle(self) -> None:
        try:
            self.reply(GREETING)
            while self._step():
                pass
        except OSError as exc:
            self.server.log(f"POP3: {self.peer} connection ended: {exc}")
        except Exception as exc:  # noqa: BLE001 — a bug must be visible, not a silently dead thread
            self.server.log(f"POP3: {self.peer} internal error: {exc!r}")

    def _step(self) -> bool:
        line = self.rfile.readline(MAX_LINE + 1)
        if not line:
            return False
        if len(line) > MAX_LINE and not line.endswith(b"\n"):
            self.reply(b"-ERR line too long")
            return False
        command, _, argument = line.decode("utf-8", "replace").strip().partition(" ")
        method = COMMANDS.get(command.upper())
        if method is None:
            return self.reply(b"-ERR unknown command")
        if command.upper() not in OPEN_COMMANDS and not self.session.authenticated:
            return self.reply(b"-ERR not logged in")
        return getattr(self, method)(argument.strip())

    def reply(self, line: bytes, payload: Optional[bytes] = None) -> bool:
        self.wfile.write(line + CRLF + (multiline_reply(payload) if payload is not None else b""))
        return True

    # -- authorization state ----------------------------------------------------
    def cmd_user(self, argument: str) -> bool:
        if self.session.authenticated:
            return self.reply(b"-ERR already logged in")
        if not argument:
            return self.reply(b"-ERR USER needs a name")
        self.session = replace(self.session, user=argument)
        return self.reply(b"+OK send PASS")

    def cmd_pass(self, argument: str) -> bool:
        session = self.session
        if session.authenticated:
            return self.reply(b"-ERR already logged in")
        if not session.user:
            return self.reply(b"-ERR USER first")
        if not self.server.config.credentials_match(session.user, argument):
            return self._login_failed()
        try:
            entries = load_entries(self.server.backend, self.server.config)
        except MailError as exc:
            self.server.log(f"POP3: {self.peer} logged in as {session.user!r} but the mailbox failed: {exc}")
            return self.reply(b"-ERR cannot open the mailbox: " + safe_text(exc))
        self.session = replace(session, authenticated=True, entries=entries)
        stubs = sum(1 for entry in entries if entry.stub is not None)
        self.server.log(f"POP3: {self.peer} logged in as {session.user!r}: {len(entries)} message(s)"
                        + (f", {stubs} above max_size" if stubs else ""))
        return self.reply(f"+OK {len(entries)} message(s)".encode("ascii"))

    def _login_failed(self) -> bool:
        attempts = self.session.attempts + 1
        self.session = replace(self.session, attempts=attempts, user="")
        limit = self.server.config.max_login_attempts
        self.server.log(f"POP3: {self.peer} failed login ({attempts}/{limit})")
        if attempts >= limit:
            self.reply(b"-ERR too many failed logins")
            return False
        return self.reply(b"-ERR invalid user name or password")

    def cmd_quit(self, _argument: str) -> bool:
        uids = self.session.deleted_uids()
        if self.session.authenticated and uids:
            try:
                self.server.backend.delete(uids)
                self.server.log(f"POP3: {self.peer} asked the backend to remove {len(uids)} message(s)")
            except MailError as exc:
                self.server.log(f"POP3: {self.peer} could not remove {len(uids)} message(s): {exc}")
                self.reply(b"-ERR some deleted messages were not removed: " + safe_text(exc))
                return False
        self.reply(b"+OK bye")
        return False

    def cmd_capa(self, _argument: str) -> bool:
        return self.reply(b"+OK capability list follows", CRLF.join(CAPABILITIES) + CRLF)

    def cmd_noop(self, _argument: str) -> bool:
        return self.reply(b"+OK")

    # -- transaction state ------------------------------------------------------
    def cmd_stat(self, _argument: str) -> bool:
        kept = self.session.kept()
        return self.reply(f"+OK {len(kept)} {sum(entry.size for entry in kept)}".encode("ascii"))

    def cmd_list(self, argument: str) -> bool:
        return self._listing(argument, lambda entry: f"{entry.number} {entry.size}")

    def cmd_uidl(self, argument: str) -> bool:
        return self._listing(argument, lambda entry: f"{entry.number} {entry.uidl}")

    def _listing(self, argument: str, render: Callable[[Entry], str]) -> bool:
        if argument:
            entry = self._lookup(argument)
            return True if entry is None else self.reply(f"+OK {render(entry)}".encode("ascii"))
        kept = self.session.kept()
        payload = "".join(render(entry) + "\r\n" for entry in kept).encode("ascii")
        head = f"+OK {len(kept)} messages ({sum(entry.size for entry in kept)} octets)"
        return self.reply(head.encode("ascii"), payload)

    def cmd_retr(self, argument: str) -> bool:
        entry = self._lookup(argument)
        if entry is None:
            return True
        raw = self._body(entry)
        if raw is None:
            return True
        self._touch(entry)
        body = ensure_final_crlf(raw)
        wire_size = len(raw) + len(dot_stuff(body)) - len(body)   # the message plus its stuffing dots
        self.server.log(f"POP3: {self.peer} sent message {entry.number} ({wire_size} bytes)")
        return self.reply(f"+OK {wire_size} octets".encode("ascii"), raw)

    def cmd_top(self, argument: str) -> bool:
        parts = argument.split()
        if len(parts) != 2 or not (parts[1].isascii() and parts[1].isdigit()):
            return self.reply(b"-ERR usage: TOP message lines")
        entry = self._lookup(parts[0])
        if entry is None:
            return True
        payload = self._top_payload(entry, int(parts[1]))
        if payload is None:
            return True
        self._touch(entry)
        return self.reply(b"+OK", payload)

    def cmd_dele(self, argument: str) -> bool:
        entry = self._lookup(argument)
        if entry is None:
            return True
        self.session = replace(self.session, deleted=self.session.deleted | {entry.number})
        return self.reply(f"+OK message {entry.number} deleted".encode("ascii"))

    def cmd_rset(self, _argument: str) -> bool:
        self.session = replace(self.session, deleted=frozenset())
        return self.reply(b"+OK")

    def cmd_last(self, _argument: str) -> bool:
        return self.reply(f"+OK {self.session.last}".encode("ascii"))

    # -- helpers -------------------------------------------------------------------
    def _lookup(self, argument: str) -> Optional[Entry]:
        if not (argument.isascii() and argument.isdigit()) or not 1 <= int(argument) <= len(self.session.entries):
            self.reply(b"-ERR no such message")
            return None
        number = int(argument)
        if number in self.session.deleted:
            self.reply(f"-ERR message {number} is deleted".encode("ascii"))
            return None
        return self.session.entries[number - 1]

    def _touch(self, entry: Entry) -> None:
        self.session = replace(self.session, last=max(self.session.last, entry.number))

    def _body(self, entry: Entry) -> Optional[bytes]:
        if entry.stub is not None:
            return entry.stub
        cached = self.session.bodies.get(entry.uid)
        if cached is not None:
            return cached
        try:
            raw = normalize_crlf(self.server.backend.fetch(entry.uid))
        except MailError as exc:
            self.server.log(f"POP3: {self.peer} cannot fetch message {entry.number}: {exc}")
            self.reply(b"-ERR cannot fetch the message: " + safe_text(exc))
            return None
        self.session = replace(self.session, bodies={**self.session.bodies, entry.uid: raw})
        return raw

    def _headers(self, entry: Entry) -> Optional[bytes]:
        cached = self.session.headers.get(entry.uid)
        if cached is not None:
            return cached
        try:
            headers = normalize_crlf(self.server.backend.fetch_headers(entry.uid)).rstrip(b"\r\n")
        except MailError as exc:
            self.server.log(f"POP3: {self.peer} cannot fetch headers of message {entry.number}: {exc}")
            self.reply(b"-ERR cannot fetch the message headers: " + safe_text(exc))
            return None
        self.session = replace(self.session, headers={**self.session.headers, entry.uid: headers})
        return headers

    def _top_payload(self, entry: Entry, count: int) -> Optional[bytes]:
        if count == 0 and entry.stub is None and entry.uid not in self.session.bodies:
            headers = self._headers(entry)
            return None if headers is None else headers + CRLF + CRLF
        raw = self._body(entry)
        if raw is None:
            return None
        headers, body = split_headers(raw)
        return headers + CRLF + CRLF + top_lines(body, count)
