"""Test doubles shared by the mail tests: an in-memory MailBackend and imaplib/smtplib look-alikes
(each records what it was asked and answers in the real library's shapes)."""
from __future__ import annotations

import imaplib
import smtplib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from jornada.sync.mail.backend import MailError, MailSummary


def message(subject: str, body: str, sender: str = "alice@example.org", to: str = "bob@example.org") -> bytes:
    text = (f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: Mon, 7 Sep 2026 10:00:00 +0000\r\n"
            f"Message-ID: <{subject.replace(' ', '.')}@example.org>\r\n\r\n{body}")
    return text.encode("utf-8")


class FakeMailBackend:
    name = "fake"

    def __init__(self, messages: Optional[Dict[str, bytes]] = None, fail_connect: Optional[str] = None,
                 fail_send: Optional[str] = None) -> None:
        self.messages = dict(messages or {})
        self.fail_connect, self.fail_send = fail_connect, fail_send
        self.fail_next: List[str] = []
        self.connects = self.closes = 0
        self.fetched: List[str] = []
        self.header_fetches: List[str] = []
        self.deleted: List[Tuple[str, ...]] = []
        self.sent: List[Tuple[bytes, str, Tuple[str, ...]]] = []

    def _maybe_fail(self, method: str) -> None:
        if self.fail_next and self.fail_next[0] == method:
            self.fail_next.pop(0)
            raise MailError(f"{method} failed (simulated)")

    def connect(self) -> None:
        self.connects += 1
        if self.fail_connect:
            raise MailError(self.fail_connect)

    def list(self) -> Tuple[MailSummary, ...]:
        self._maybe_fail("list")
        return tuple(MailSummary(uid, len(raw)) for uid, raw in self.messages.items())

    def _raw(self, uid: str) -> bytes:
        try:
            return self.messages[uid]
        except KeyError:
            raise MailError(f"no message {uid}") from None

    def fetch(self, uid: str) -> bytes:
        self._maybe_fail("fetch")
        self.fetched.append(uid)
        return self._raw(uid)

    def fetch_headers(self, uid: str) -> bytes:
        self.header_fetches.append(uid)
        return self._raw(uid).split(b"\r\n\r\n", 1)[0]

    def delete(self, uids: Sequence[str]) -> None:
        self._maybe_fail("delete")
        self.deleted.append(tuple(uids))
        for uid in uids:
            self.messages.pop(uid, None)

    def send(self, raw: bytes, sender: str, recipients: Sequence[str]) -> None:
        if self.fail_send:
            raise MailError(self.fail_send)
        self.sent.append((raw, sender, tuple(recipients)))

    def close(self) -> None:
        self.closes += 1


class FakeImap:
    """Enough of imaplib.IMAP4 for ImapBackend; answers use imaplib's (typ, data) shapes."""

    error = imaplib.IMAP4.error

    def __init__(self, host: str, port: int, use_ssl: bool, messages: Dict[str, bytes], fail_login: bool = False,
                 missing_mailboxes: Iterable[str] = ()) -> None:
        self.host, self.port, self.use_ssl = host, port, use_ssl
        self.messages = dict(messages)
        self.fail_login = fail_login
        self.missing_mailboxes = set(missing_mailboxes)
        self.calls: List[Tuple[Any, ...]] = []
        self.flags: Dict[str, str] = {}
        self.expunged: List[str] = []

    def starttls(self, ssl_context: Any = None) -> Tuple[str, list]:
        self.calls.append(("starttls",))
        return "OK", [b""]

    def login(self, user: str, password: str) -> Tuple[str, list]:
        self.calls.append(("login", user, password))
        if self.fail_login:
            raise self.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"LOGIN completed"]

    def authenticate(self, mechanism: str, authobject: Any) -> Tuple[str, list]:
        self.calls.append(("authenticate", mechanism, authobject(b"")))
        if self.fail_login:
            raise self.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"Success"]

    def select(self, mailbox: str, readonly: bool = False) -> Tuple[str, list]:
        self.calls.append(("select", mailbox))
        if mailbox.strip('"') in self.missing_mailboxes:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        return "OK", [str(len(self.messages)).encode("ascii")]

    def uid(self, command: str, *args: Any) -> Tuple[str, list]:
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", [b" ".join(uid.encode("ascii") for uid in self.messages)]
        if command == "FETCH":
            return self._fetch(args[0], args[1])
        if command == "STORE":
            for uid in args[0].split(","):
                self.flags[uid] = args[2]
            return "OK", [b""]
        if command == "COPY":
            if args[1].strip('"') in self.missing_mailboxes:
                return "NO", [b"[TRYCREATE] No such mailbox"]
            return "OK", [b"COPY completed"]
        raise self.error(f"unexpected UID {command}")

    def _fetch(self, uid_set: str, part: str) -> Tuple[str, list]:
        uids = uid_set.split(",")
        if "RFC822.SIZE" in part:
            return "OK", [f"{index} (UID {uid} RFC822.SIZE {len(self.messages[uid])})".encode("ascii")
                          for index, uid in enumerate(uids, 1) if uid in self.messages]
        raw = self.messages.get(uids[0])
        if raw is None:
            return "OK", [None]
        payload = raw if "[]" in part else raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        head = f"1 (UID {uids[0]} {part.strip('()').replace('.PEEK', '')} {{{len(payload)}}}".encode("ascii")
        return "OK", [(head, payload), b")"]

    def expunge(self) -> Tuple[str, list]:
        self.calls.append(("expunge",))
        removed = [uid for uid, flags in self.flags.items() if "Deleted" in flags]
        for uid in removed:
            self.messages.pop(uid, None)
        self.expunged.extend(removed)
        return "OK", [b"1"]

    def create(self, mailbox: str) -> Tuple[str, list]:
        self.calls.append(("create", mailbox))
        self.missing_mailboxes.discard(mailbox.strip('"'))
        return "OK", [b""]

    def logout(self) -> Tuple[str, list]:
        self.calls.append(("logout",))
        return "BYE", [b"LOGOUT"]


class FakeSmtp:
    """Enough of smtplib.SMTP for ImapBackend.send."""

    def __init__(self, host: str, port: int, use_ssl: bool, refuse: Iterable[str] = (),
                 fail_auth: bool = False) -> None:
        self.host, self.port, self.use_ssl = host, port, use_ssl
        self.refuse, self.fail_auth = set(refuse), fail_auth
        self.calls: List[Tuple[Any, ...]] = []
        self.sent: List[Tuple[str, Tuple[str, ...], bytes]] = []

    def ehlo(self) -> Tuple[int, bytes]:
        self.calls.append(("ehlo",))
        return 250, b"ok"

    def starttls(self, context: Any = None) -> Tuple[int, bytes]:
        self.calls.append(("starttls",))
        return 220, b"ready"

    def login(self, user: str, password: str) -> Tuple[int, bytes]:
        self.calls.append(("login", user, password))
        if self.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        return 235, b"ok"

    def auth(self, mechanism: str, authobject: Any, initial_response_ok: bool = True) -> Tuple[int, bytes]:
        self.calls.append(("auth", mechanism, authobject(), authobject(b"eyJzdGF0dXMiOiI0MDEifQ==")))
        if self.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Invalid token")
        return 235, b"ok"

    def sendmail(self, sender: str, recipients: Sequence[str], raw: bytes) -> Dict[str, Tuple[int, bytes]]:
        refused = {address: (550, b"5.1.1 unknown") for address in recipients if address in self.refuse}
        if refused and len(refused) == len(recipients):
            raise smtplib.SMTPRecipientsRefused(refused)
        self.sent.append((sender, tuple(recipients), raw))
        return refused

    def quit(self) -> Tuple[int, bytes]:
        self.calls.append(("quit",))
        return 221, b"bye"


def imap_factory(messages: Dict[str, bytes], **options: Any) -> Any:
    created: List[FakeImap] = []

    def factory(host: str, port: int, use_ssl: bool) -> FakeImap:
        client = FakeImap(host, port, use_ssl, messages, **options)
        created.append(client)
        return client

    factory.created = created  # type: ignore[attr-defined]
    return factory


def smtp_factory(**options: Any) -> Any:
    created: List[FakeSmtp] = []

    def factory(host: str, port: int, use_ssl: bool) -> FakeSmtp:
        client = FakeSmtp(host, port, use_ssl, **options)
        created.append(client)
        return client

    factory.created = created  # type: ignore[attr-defined]
    return factory


def test_fake_backend_records_calls():
    backend = FakeMailBackend({"1": message("One", "body")})
    backend.connect()
    assert backend.list() == (MailSummary("1", len(message("One", "body"))),)
    assert backend.fetch_headers("1").endswith(b"<One@example.org>")
    backend.delete(("1",))
    assert backend.deleted == [("1",)] and backend.messages == {}
