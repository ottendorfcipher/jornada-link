"""IMAP (list, fetch, delete) and SMTP (send) against a real mailbox, with injectable client
factories so tests run without a network. Authentication is a callable given the connected
client, which is how password logins and XOAUTH2 share this code."""
from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, TypeVar

from ..accounts import AccountError
from ..registry import BuildContext
from .backend import DELETE_ARCHIVE, DELETE_DELETE, MailError, MailLimits, MailSummary

NETWORK_TIMEOUT = 60.0
SSL = "ssl"
STARTTLS = "starttls"
SECURITIES = (SSL, STARTTLS)
IMAP_FACTORY_HOOK = "imap_factory"
SMTP_FACTORY_HOOK = "smtp_factory"
DEFAULT_MAILBOX = "INBOX"
UID_RE = re.compile(rb"UID (\d+)")
SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")

T = TypeVar("T")
ClientFactory = Callable[[str, int, bool], Any]
Authenticator = Callable[[Any], None]


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    security: Optional[str] = None   # SSL / STARTTLS; None = by port (465 and 993 → SSL)

    @property
    def use_ssl(self) -> bool:
        if self.security is None:
            return self.port in (465, 993)
        return self.security == SSL


def default_imap_factory(host: str, port: int, use_ssl: bool) -> Any:
    if use_ssl:
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=NETWORK_TIMEOUT)
    return imaplib.IMAP4(host, port, timeout=NETWORK_TIMEOUT)


def default_smtp_factory(host: str, port: int, use_ssl: bool) -> Any:
    if use_ssl:
        return smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=NETWORK_TIMEOUT)
    return smtplib.SMTP(host, port, timeout=NETWORK_TIMEOUT)


def factories_from_context(context: BuildContext) -> Tuple[ClientFactory, ClientFactory]:
    """The real imaplib/smtplib constructors unless the context's test hooks replace them."""
    return (context.extra.get(IMAP_FACTORY_HOOK) or default_imap_factory,
            context.extra.get(SMTP_FACTORY_HOOK) or default_smtp_factory)


def password_login(user: str, password: str) -> Authenticator:
    return lambda client: client.login(user, password)


def quote_mailbox(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def validate_uid(uid: str) -> str:
    if not (uid.isascii() and uid.isdigit()):
        raise MailError(f"bad message id {uid!r}")
    return uid


def response_text(data: Any) -> str:
    parts = [item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
             for item in (data or ()) if item is not None]
    return " ".join(parts)[:200]


def check(result: Tuple[str, Any], what: str) -> Any:
    typ, data = result
    if typ != "OK":
        raise MailError(f"{what}: the server answered {typ} {response_text(data)}".rstrip())
    return data


def reason(exc: Any) -> str:
    """A short, secret-free description of a library error (or of a raw server reply)."""
    parts = list(exc.args) if isinstance(exc, BaseException) and exc.args else [exc]
    text = " ".join(part.decode("utf-8", "replace") if isinstance(part, bytes) else str(part) for part in parts)
    return " ".join(text.split())[:200] or type(exc).__name__


def parse_uids(data: Any) -> Tuple[str, ...]:
    text = b" ".join(item for item in (data or ()) if isinstance(item, bytes)).decode("ascii", "replace")
    return tuple(uid for uid in text.split() if uid.isdigit())


def parse_sizes(data: Any) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    for item in data or ():
        if not isinstance(item, bytes):
            continue
        uid, size = UID_RE.search(item), SIZE_RE.search(item)
        if uid and size:
            sizes = {**sizes, uid.group(1).decode("ascii"): int(size.group(1))}
    return sizes


def literal_payload(data: Any) -> Optional[bytes]:
    for item in data or ():
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


class ImapBackend:
    """A mailbox reached over IMAP, sending through SMTP; both TLS-only with certificate checks."""

    def __init__(self, name: str, imap: Endpoint, smtp: Endpoint, mailbox: str, limits: MailLimits,
                 imap_login: Authenticator, smtp_login: Authenticator, default_sender: str = "",
                 trash_mailbox: Optional[str] = None, imap_factory: ClientFactory = default_imap_factory,
                 smtp_factory: ClientFactory = default_smtp_factory) -> None:
        self.name = name
        self._imap, self._smtp, self._mailbox, self._limits = imap, smtp, mailbox, limits
        self._imap_login, self._smtp_login = imap_login, smtp_login
        self._default_sender, self._trash_mailbox = default_sender, trash_mailbox
        self._imap_factory, self._smtp_factory = imap_factory, smtp_factory
        self._client: Optional[Any] = None

    # -- connection ------------------------------------------------------------------
    def connect(self) -> None:
        self.close()
        self._client = self._guard("connecting", self._open)

    def _open(self) -> Any:
        client = self._imap_factory(self._imap.host, self._imap.port, self._imap.use_ssl)
        if not self._imap.use_ssl:
            client.starttls(ssl_context=ssl.create_default_context())
        self._imap_login(client)
        check(client.select(quote_mailbox(self._mailbox)), f"opening mailbox {self._mailbox!r}")
        return client

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def _connection(self) -> Any:
        if self._client is None:
            raise MailError(f"not connected to {self._imap.host}")
        return self._client

    def _guard(self, what: str, action: Callable[[], T]) -> T:
        try:
            return action()
        except MailError:
            raise
        except AccountError as exc:
            raise MailError(str(exc)) from exc
        except (imaplib.IMAP4.error, OSError, ValueError) as exc:
            raise MailError(f"{what} on {self._imap.host}: {reason(exc)}") from exc

    # -- reading -------------------------------------------------------------------------
    def list(self) -> Tuple[MailSummary, ...]:
        client = self._connection()

        def action() -> Tuple[MailSummary, ...]:
            uids = parse_uids(check(client.uid("SEARCH", None, "ALL"), "listing messages"))
            newest = uids[-self._limits.max_messages:]
            if not newest:
                return ()
            sizes = parse_sizes(check(client.uid("FETCH", ",".join(newest), "(RFC822.SIZE)"), "reading sizes"))
            return tuple(MailSummary(uid, sizes[uid]) for uid in newest if uid in sizes)

        return self._guard("listing messages", action)

    def fetch(self, uid: str) -> bytes:
        return self._fetch_part(uid, "(BODY.PEEK[])")

    def fetch_headers(self, uid: str) -> bytes:
        return self._fetch_part(uid, "(BODY.PEEK[HEADER])")

    def _fetch_part(self, uid: str, part: str) -> bytes:
        client = self._connection()
        validate_uid(uid)

        def action() -> bytes:
            payload = literal_payload(check(client.uid("FETCH", uid, part), f"fetching message {uid}"))
            if payload is None:
                raise MailError(f"message {uid} is no longer in the mailbox")
            return payload

        return self._guard(f"fetching message {uid}", action)

    # -- deleting -------------------------------------------------------------------------
    def delete(self, uids: Sequence[str]) -> None:
        mode = self._limits.on_delete
        if not uids or mode not in (DELETE_DELETE, DELETE_ARCHIVE):
            return
        joined = ",".join(validate_uid(uid) for uid in uids)
        client = self._connection()
        target = self._limits.archive_mailbox if mode == DELETE_ARCHIVE else self._trash_mailbox

        def action() -> None:
            if target:
                self._copy(client, joined, target)
            check(client.uid("STORE", joined, "+FLAGS.SILENT", "(\\Deleted)"), "flagging messages as deleted")
            check(client.expunge(), "expunging messages")

        self._guard("removing messages", action)

    @staticmethod
    def _copy(client: Any, joined: str, target: str) -> None:
        quoted = quote_mailbox(target)
        typ, _data = client.uid("COPY", joined, quoted)
        if typ == "OK":
            return
        check(client.create(quoted), f"creating mailbox {target!r}")
        check(client.uid("COPY", joined, quoted), f"copying to mailbox {target!r}")

    # -- sending ---------------------------------------------------------------------------
    def send(self, raw: bytes, sender: str, recipients: Sequence[str]) -> None:
        if not recipients:
            raise MailError("the message has no recipients")
        host = self._smtp.host
        try:
            client = self._smtp_factory(host, self._smtp.port, self._smtp.use_ssl)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"cannot reach SMTP server {host}: {reason(exc)}") from exc
        try:
            self._submit(client, raw, sender or self._default_sender, tuple(recipients))
        except AccountError as exc:
            raise MailError(str(exc)) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(f"{host} refused the recipient(s): {', '.join(exc.recipients)}") from exc
        except smtplib.SMTPResponseException as exc:
            raise MailError(f"{host} answered {exc.smtp_code} {reason(exc.smtp_error)}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"sending through {host} failed: {reason(exc)}") from exc
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                pass

    def _submit(self, client: Any, raw: bytes, sender: str, recipients: Tuple[str, ...]) -> None:
        client.ehlo()
        if not self._smtp.use_ssl:
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        self._smtp_login(client)
        refused = client.sendmail(sender, list(recipients), raw)
        if refused:
            raise MailError(f"{self._smtp.host} refused the recipient(s): {', '.join(refused)}")
