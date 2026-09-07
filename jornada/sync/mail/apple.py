"""Backend ``apple``: Mail.app through JavaScript for Automation. Messages are rebuilt as plain
RFC 2822 text from what Mail exposes (subject, addresses, date, text content); attachments
are not carried in either direction."""
from __future__ import annotations

import email.policy
import json
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, parseaddr
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ...webapi.applescript import AppleScriptError, Runner, run_jxa
from ..accounts import Account
from ..registry import BackendSpec, BuildContext, SettingSpec
from .backend import DELETE_KEEP, MailError, MailLimits, MailSummary, limits_from_account, shared_settings
from .messages import split_headers
from . import apple_scripts

DEFAULT_MAILBOX = "INBOX"
JS_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")
MAX_REPORTED_ERRORS = 3
SETTINGS = (
    SettingSpec("account", "Mail account name (default: every account)", required=False),
    SettingSpec("mailbox", f"mailbox offered to the device (default {DEFAULT_MAILBOX})", required=False,
                default=DEFAULT_MAILBOX),
    SettingSpec("sender", "From address for mail sent from the device (default: Mail's own default)",
                required=False),
) + shared_settings()


def header_text(value: Any) -> str:
    """A header value without line breaks (Mail data never carries CR/LF legitimately)."""
    return " ".join(str(value or "").split())


def address_header(value: Any) -> str:
    name, address = parseaddr(header_text(value))
    return formataddr((name, address)) if address else header_text(value)


def recipient_header(items: Any) -> str:
    pairs = [(header_text(item.get("name")), header_text(item.get("address")))
             for item in (items or ()) if isinstance(item, dict) and item.get("address")]
    return ", ".join(formataddr(pair) for pair in pairs)


def parse_js_date(value: Any) -> Optional[datetime]:
    text = str(value or "")
    for pattern in JS_DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def message_id_header(value: Any) -> str:
    text = header_text(value)
    if not text or " " in text:
        return ""
    return text if text.startswith("<") else f"<{text}>"


def build_message(item: Mapping[str, Any]) -> bytes:
    """An RFC 2822 message (CRLF, plain text) from what the LIST script returned for one message."""
    message = EmailMessage()
    message["From"] = address_header(item.get("sender")) or "unknown@jornada.invalid"
    message["To"] = recipient_header(item.get("to")) or "undisclosed-recipients:;"
    cc = recipient_header(item.get("cc"))
    if cc:
        message["Cc"] = cc
    message["Subject"] = header_text(item.get("subject"))
    message["Date"] = format_datetime(parse_js_date(item.get("date")) or datetime.now(timezone.utc))
    message_id = message_id_header(item.get("messageId"))
    if message_id:
        message["Message-ID"] = message_id
    message.set_content(str(item.get("content") or ""))
    return message.as_bytes(policy=email.policy.SMTP)


def text_of(raw: bytes) -> Tuple[str, str]:
    """(subject, plain-text body) of a message the device sent."""
    parsed = message_from_bytes(raw, policy=email.policy.default)
    body = parsed.get_body(preferencelist=("plain",))
    if body is None:
        return header_text(parsed["Subject"]), ""
    try:
        text = body.get_content()
    except (LookupError, UnicodeDecodeError, ValueError, KeyError):
        text = (body.get_payload(decode=True) or b"").decode("latin-1")
    return header_text(parsed["Subject"]), str(text).replace("\r\n", "\n")


class AppleMailBackend:
    name = "apple"

    def __init__(self, account_name: str, mailbox: str, sender: str, limits: MailLimits,
                 runner: Optional[Runner] = None) -> None:
        self._account, self._mailbox, self._sender, self._limits = account_name, mailbox, sender, limits
        self._runner = runner
        self._cache: Mapping[str, bytes] = {}

    def _run(self, script: str, args: Sequence[str]) -> Any:
        try:
            return run_jxa(script, list(args), runner=self._runner)
        except AppleScriptError as exc:
            raise MailError(f"Mail.app: {exc}") from exc

    def connect(self) -> None:
        result = self._run(apple_scripts.CONNECT, (self._account, self._mailbox))
        count = result.get("mailboxes", 0) if isinstance(result, dict) else 0
        if not count:
            where = f" of account {self._account!r}" if self._account else ""
            raise MailError(f"Mail.app has no mailbox {self._mailbox!r}{where}")

    def list(self) -> Tuple[MailSummary, ...]:
        items = self._run(apple_scripts.LIST, (self._account, self._mailbox, str(self._limits.max_messages)))
        if not isinstance(items, list):
            raise MailError("Mail.app returned an unexpected message list")
        built = {str(item["id"]): build_message(item)
                 for item in items if isinstance(item, dict) and item.get("id") is not None and not item.get("error")}
        self._cache = built
        return tuple(MailSummary(uid, len(raw)) for uid, raw in built.items())

    def fetch(self, uid: str) -> bytes:
        raw = self._cache.get(uid)
        if raw is None:
            self.list()
            raw = self._cache.get(uid)
        if raw is None:
            raise MailError(f"message {uid} is no longer in the mailbox")
        return raw

    def fetch_headers(self, uid: str) -> bytes:
        return split_headers(self.fetch(uid))[0]

    def delete(self, uids: Sequence[str]) -> None:
        if not uids:
            return
        mode = self._limits.on_delete
        args = (self._account, self._mailbox, mode, self._limits.archive_mailbox, json.dumps([int(uid) for uid in uids]))
        result = self._run(apple_scripts.DELETE, args)
        errors = result.get("errors") or () if isinstance(result, dict) else ()
        if errors:
            shown = "; ".join(str(error) for error in errors[:MAX_REPORTED_ERRORS])
            raise MailError(f"Mail.app could not {mode if mode != DELETE_KEEP else 'mark'} "
                            f"{len(errors)} message(s): {shown}")
        self._cache = {uid: raw for uid, raw in self._cache.items() if uid not in set(uids)}

    def send(self, raw: bytes, sender: str, recipients: Sequence[str]) -> None:
        if not recipients:
            raise MailError("the message has no recipients")
        subject, text = text_of(raw)
        result = self._run(apple_scripts.SEND, (subject, text, self._sender, json.dumps(list(recipients))))
        if not (isinstance(result, dict) and result.get("sent")):
            raise MailError("Mail.app did not send the message")

    def close(self) -> None:
        return


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> AppleMailBackend:
    del secrets  # Mail.app holds the real credentials
    return AppleMailBackend(
        account_name=(account.setting("account") or "").strip(),
        mailbox=(account.setting("mailbox") or "").strip() or DEFAULT_MAILBOX,
        sender=(account.setting("sender") or "").strip(),
        limits=limits_from_account(account),
        runner=context.runner,
    )


BACKEND = BackendSpec(
    "apple", "Apple Mail (Mail.app on this Mac)", SETTINGS, build,
    notes="needs Automation permission for Mail; messages reach the device as plain text without attachments.",
)
