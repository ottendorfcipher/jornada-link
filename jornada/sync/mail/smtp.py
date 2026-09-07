"""A small SMTP receiver (RFC 5321, AUTH PLAIN/LOGIN per RFC 4954) that hands every accepted
message to a mail backend. Credentials are compared and forgotten, never logged."""
from __future__ import annotations

import base64
import binascii
import re
import socketserver
from dataclasses import dataclass, replace
from typing import Callable, Optional, Tuple

from .backend import MailBackend, MailError
from .config import BRIDGE_NAME, BridgeConfig
from .messages import CRLF, dot_unstuff

Log = Callable[[str], None]
MAX_COMMAND_LINE = 512
MAX_DATA_LINE = 1_000_000
READ_CHUNK = 65536
MAX_RECIPIENTS = 100
MAX_ADDRESS = 320
PATH = re.compile(r"^(FROM|TO):\s*(<[^<>]*>|[^\s<>]+)\s*(.*)$", re.IGNORECASE)
SIZE_PARAM = re.compile(r"\bSIZE=(\d+)", re.IGNORECASE)
COMMANDS = {"EHLO": "cmd_ehlo", "HELO": "cmd_helo", "AUTH": "cmd_auth", "MAIL": "cmd_mail", "RCPT": "cmd_rcpt",
            "DATA": "cmd_data", "RSET": "cmd_rset", "NOOP": "cmd_noop", "QUIT": "cmd_quit", "VRFY": "cmd_vrfy",
            "HELP": "cmd_help", "STARTTLS": "cmd_starttls"}
USERNAME_CHALLENGE = b"334 VXNlcm5hbWU6"
PASSWORD_CHALLENGE = b"334 UGFzc3dvcmQ6"


def silent(_line: str) -> None:
    return


def safe_text(value: object) -> bytes:
    return " ".join(str(value).split())[:200].encode("ascii", "replace")


def decode_base64(text: str) -> Optional[str]:
    try:
        return base64.b64decode(text.strip(), validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None


def parse_path(argument: str, keyword: str) -> Optional[Tuple[str, str]]:
    """``FROM:<addr> params`` / ``TO:<addr>`` → (address, parameters); None when malformed."""
    match = PATH.match(argument)
    if not match or match.group(1).upper() != keyword:
        return None
    return match.group(2).strip("<>").strip(), match.group(3)


def valid_address(address: str) -> bool:
    return (0 < len(address) <= MAX_ADDRESS and "@" in address
            and not any(ch.isspace() or ord(ch) < 32 for ch in address))


@dataclass(frozen=True)
class PendingAuth:
    mechanism: str
    user: Optional[str] = None


@dataclass(frozen=True)
class SmtpSession:
    helo: str = ""
    authenticated: bool = False
    failures: int = 0
    sender: Optional[str] = None
    recipients: Tuple[str, ...] = ()
    pending: Optional[PendingAuth] = None

    def reset(self) -> "SmtpSession":
        return replace(self, sender=None, recipients=(), pending=None)


class SmtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: Tuple[str, int], config: BridgeConfig, backend: MailBackend,
                 log: Log = silent) -> None:
        self.config = config
        self.backend = backend
        self.log = log
        super().__init__(address, SmtpHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])


class SmtpHandler(socketserver.StreamRequestHandler):
    server: SmtpServer
    disable_nagle_algorithm = True

    def setup(self) -> None:
        self.timeout = self.server.config.session_timeout
        super().setup()
        self.session = SmtpSession()
        self.peer = f"{self.client_address[0]}:{self.client_address[1]}"

    def handle(self) -> None:
        try:
            self.reply(f"220 {BRIDGE_NAME} Jornada mail bridge ESMTP".encode("ascii"))
            while self._step():
                pass
        except EOFError:
            self.server.log(f"SMTP: {self.peer} disconnected during DATA")
        except OSError as exc:
            self.server.log(f"SMTP: {self.peer} connection ended: {exc}")
        except Exception as exc:  # noqa: BLE001 — a bug must be visible, not a silently dead thread
            self.server.log(f"SMTP: {self.peer} internal error: {exc!r}")

    def _step(self) -> bool:
        line = self.rfile.readline(MAX_COMMAND_LINE + 1)
        if not line:
            return False
        if len(line) > MAX_COMMAND_LINE and not line.endswith(b"\n"):
            self.reply(b"500 5.5.2 line too long")
            return False
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if self.session.pending is not None:
            return self._auth_step(text)
        command, _, argument = text.partition(" ")
        method = COMMANDS.get(command.upper())
        if method is None:
            return self.reply(b"500 5.5.1 command not recognized")
        return getattr(self, method)(argument.strip())

    def reply(self, line: bytes) -> bool:
        self.wfile.write(line + CRLF)
        return True

    # -- session commands ----------------------------------------------------------
    def cmd_ehlo(self, argument: str) -> bool:
        if not argument:
            return self.reply(b"501 5.5.4 EHLO needs a host name")
        self.session = replace(self.session.reset(), helo=argument)
        lines = (b"250-" + BRIDGE_NAME.encode("ascii") + b" hello " + safe_text(argument),
                 f"250-SIZE {self.server.config.smtp_max_message}".encode("ascii"),
                 b"250-AUTH PLAIN LOGIN", b"250 8BITMIME")
        return self.reply(CRLF.join(lines))

    def cmd_helo(self, argument: str) -> bool:
        if not argument:
            return self.reply(b"501 5.5.4 HELO needs a host name")
        self.session = replace(self.session.reset(), helo=argument)
        return self.reply(b"250 " + BRIDGE_NAME.encode("ascii") + b" hello " + safe_text(argument))

    def cmd_rset(self, _argument: str) -> bool:
        self.session = self.session.reset()
        return self.reply(b"250 2.0.0 OK")

    def cmd_noop(self, _argument: str) -> bool:
        return self.reply(b"250 2.0.0 OK")

    def cmd_quit(self, _argument: str) -> bool:
        self.reply(b"221 2.0.0 bye")
        return False

    def cmd_vrfy(self, _argument: str) -> bool:
        return self.reply(b"252 2.5.2 cannot verify, send to the address instead")

    def cmd_help(self, _argument: str) -> bool:
        return self.reply(b"214 2.0.0 commands: EHLO HELO AUTH MAIL RCPT DATA RSET NOOP QUIT")

    def cmd_starttls(self, _argument: str) -> bool:
        return self.reply(b"502 5.5.1 STARTTLS is not offered on the PPP link")

    # -- authentication -------------------------------------------------------------
    def cmd_auth(self, argument: str) -> bool:
        if self.session.authenticated:
            return self.reply(b"503 5.5.1 already authenticated")
        mechanism, _, initial = argument.partition(" ")
        mechanism, initial = mechanism.upper(), initial.strip()
        if mechanism == "PLAIN":
            if initial:
                return self._finish_plain(initial)
            self.session = replace(self.session, pending=PendingAuth("PLAIN"))
            return self.reply(b"334 ")
        if mechanism == "LOGIN":
            if not initial:
                self.session = replace(self.session, pending=PendingAuth("LOGIN"))
                return self.reply(USERNAME_CHALLENGE)
            return self._login_user(initial)
        return self.reply(b"504 5.5.4 mechanism not supported (use PLAIN or LOGIN)")

    def _auth_step(self, text: str) -> bool:
        pending = self.session.pending
        self.session = replace(self.session, pending=None)
        if pending is None:
            return self.reply(b"503 5.5.1 no authentication in progress")
        if text.strip() == "*":
            return self.reply(b"501 5.7.0 authentication aborted")
        if pending.mechanism == "PLAIN":
            return self._finish_plain(text)
        if pending.user is None:
            return self._login_user(text)
        password = decode_base64(text)
        if password is None:
            return self.reply(b"501 5.5.2 cannot decode the response")
        return self._check(pending.user, password)

    def _login_user(self, encoded: str) -> bool:
        user = decode_base64(encoded)
        if user is None:
            return self.reply(b"501 5.5.2 cannot decode the response")
        self.session = replace(self.session, pending=PendingAuth("LOGIN", user))
        return self.reply(PASSWORD_CHALLENGE)

    def _finish_plain(self, encoded: str) -> bool:
        decoded = decode_base64(encoded)
        if decoded is None:
            return self.reply(b"501 5.5.2 cannot decode the response")
        parts = decoded.split("\0")
        if len(parts) != 3:
            return self.reply(b"501 5.5.2 malformed AUTH PLAIN response")
        return self._check(parts[1], parts[2])

    def _check(self, user: str, password: str) -> bool:
        if self.server.config.credentials_match(user, password):
            self.session = replace(self.session, authenticated=True)
            self.server.log(f"SMTP: {self.peer} authenticated as {user!r}")
            return self.reply(b"235 2.7.0 authentication successful")
        failures = self.session.failures + 1
        limit = self.server.config.max_login_attempts
        self.session = replace(self.session, failures=failures)
        self.server.log(f"SMTP: {self.peer} failed authentication ({failures}/{limit})")
        if failures >= limit:
            self.reply(b"421 4.7.0 too many failed logins, closing")
            return False
        return self.reply(b"535 5.7.8 authentication failed")

    # -- the envelope and the message ------------------------------------------------
    def cmd_mail(self, argument: str) -> bool:
        if self.server.config.smtp_auth and not self.session.authenticated:
            return self.reply(b"530 5.7.0 authentication required")
        if self.session.sender is not None:
            return self.reply(b"503 5.5.1 nested MAIL command, send RSET first")
        parsed = parse_path(argument, "FROM")
        if parsed is None:
            return self.reply(b"501 5.5.4 syntax: MAIL FROM:<address>")
        address, parameters = parsed
        declared = SIZE_PARAM.search(parameters)
        if declared and int(declared.group(1)) > self.server.config.smtp_max_message:
            return self.reply(b"552 5.3.4 message exceeds the size limit")
        if address and not valid_address(address):
            return self.reply(b"501 5.1.7 bad sender address")
        self.session = replace(self.session, sender=address, recipients=())
        return self.reply(b"250 2.1.0 sender OK")

    def cmd_rcpt(self, argument: str) -> bool:
        if self.session.sender is None:
            return self.reply(b"503 5.5.1 MAIL first")
        if len(self.session.recipients) >= MAX_RECIPIENTS:
            return self.reply(b"452 4.5.3 too many recipients")
        parsed = parse_path(argument, "TO")
        if parsed is None or not parsed[0]:
            return self.reply(b"501 5.5.4 syntax: RCPT TO:<address>")
        if not valid_address(parsed[0]):
            return self.reply(b"501 5.1.3 bad recipient address")
        self.session = replace(self.session, recipients=self.session.recipients + (parsed[0],))
        return self.reply(b"250 2.1.5 recipient OK")

    def cmd_data(self, _argument: str) -> bool:
        session = self.session
        if session.sender is None:
            return self.reply(b"503 5.5.1 MAIL first")
        if not session.recipients:
            return self.reply(b"503 5.5.1 RCPT first")
        self.reply(b"354 end data with <CR><LF>.<CR><LF>")
        raw = self._read_data()
        self.session = session.reset()
        if raw is None:
            return self.reply(b"552 5.3.4 message exceeds the size limit")
        return self._deliver(raw, session.sender, session.recipients)

    def _read_data_line(self) -> Optional[bytes]:
        collected = b""
        while True:
            piece = self.rfile.readline(READ_CHUNK)
            if not piece:
                return None
            collected += piece
            if piece.endswith(b"\n") or len(collected) > MAX_DATA_LINE:
                return collected

    def _read_data(self) -> Optional[bytes]:
        """The message up to the lone dot, unstuffed and CRLF-normalised; None when above the cap."""
        lines = []
        total, too_big = 0, False
        while True:
            line = self._read_data_line()
            if line is None:
                raise EOFError("connection closed during DATA")
            content = line.rstrip(b"\r\n")
            if content == b".":
                break
            content = dot_unstuff(content)
            total += len(content) + len(CRLF)
            if total > self.server.config.smtp_max_message:
                too_big = True
                continue
            lines.append(content)
        return None if too_big else b"".join(line + CRLF for line in lines)

    def _deliver(self, raw: bytes, sender: str, recipients: Tuple[str, ...]) -> bool:
        try:
            self.server.backend.send(raw, sender, recipients)
        except MailError as exc:
            self.server.log(f"SMTP: {self.peer} message from {sender or '<>'} not sent: {exc}")
            return self.reply(b"451 4.3.0 could not send: " + safe_text(exc))
        except Exception as exc:  # noqa: BLE001 — a backend bug must not kill the session
            self.server.log(f"SMTP: {self.peer} message from {sender or '<>'} failed: {exc!r}")
            return self.reply(b"554 5.3.0 delivery failed: " + safe_text(exc))
        self.server.log(f"SMTP: {self.peer} sent {len(raw)} bytes from {sender or '<>'} "
                        f"to {len(recipients)} recipient(s)")
        return self.reply(b"250 2.0.0 message accepted for delivery")
