"""What a mail backend looks like to the bridge, and the settings every backend shares.

The bridge speaks POP3/SMTP to the device; a backend speaks to the real mailbox
(IMAP, Gmail, Microsoft 365, Mail.app). A backend never sees the device: it
lists, fetches, deletes and sends whole RFC 2822 messages.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, Tuple

from ..accounts import Account
from ..registry import SettingSpec

DEFAULT_MAX_MESSAGES = 50
DEFAULT_MAX_SIZE = 262144
DEFAULT_ARCHIVE_MAILBOX = "Archive"
DELETE_KEEP = "keep"
DELETE_DELETE = "delete"
DELETE_ARCHIVE = "archive"
DELETE_MODES = (DELETE_KEEP, DELETE_DELETE, DELETE_ARCHIVE)
MAX_PORT = 65535


class MailError(RuntimeError):
    """A backend could not reach or change the real mailbox (message is safe to show; contains no secrets)."""


@dataclass(frozen=True)
class MailSummary:
    """One message as the backend lists it: a stable id and the size it will serve."""

    uid: str
    size: int


class MailBackend(Protocol):
    """Implemented by every mail backend; ``uid`` values are opaque, stable strings."""

    name: str

    def connect(self) -> None: ...

    def list(self) -> Tuple[MailSummary, ...]: ...

    def fetch(self, uid: str) -> bytes: ...

    def fetch_headers(self, uid: str) -> bytes: ...

    def delete(self, uids: Sequence[str]) -> None: ...

    def send(self, raw: bytes, sender: str, recipients: Sequence[str]) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class MailLimits:
    """The shared per-account choices a backend honours."""

    max_messages: int = DEFAULT_MAX_MESSAGES
    on_delete: str = DELETE_KEEP
    archive_mailbox: str = DEFAULT_ARCHIVE_MAILBOX


def shared_settings(archive_default: str = DEFAULT_ARCHIVE_MAILBOX) -> Tuple[SettingSpec, ...]:
    """The settings every mail backend declares: the device's local login and the limits."""
    return (
        SettingSpec("local_user", "user name the device's Inbox logs in with (any ASCII word, e.g. jornada)"),
        SettingSpec("local_password", "password the device's Inbox logs in with (ASCII; not your real password)",
                    secret=True),
        SettingSpec("max_messages", f"newest N messages offered to the device (default {DEFAULT_MAX_MESSAGES})",
                    required=False, default=str(DEFAULT_MAX_MESSAGES)),
        SettingSpec("max_size", "messages larger than this many bytes reach the device as a short stub "
                    f"(default {DEFAULT_MAX_SIZE})", required=False, default=str(DEFAULT_MAX_SIZE)),
        SettingSpec("on_delete", "what deleting on the device does to the real message: keep (default), "
                    "delete or archive", required=False, default=DELETE_KEEP),
        SettingSpec("archive_mailbox", f"folder used by on_delete=archive (default {archive_default})",
                    required=False, default=archive_default),
    )


# -- setting parsers (fail fast with a message that names the setting) ---------
def required_setting(account: Account, key: str) -> str:
    value = (account.setting(key) or "").strip()
    if not value:
        raise MailError(f"account {account.name!r} needs the setting {key!r} "
                        f"(`jornada sync account add {account.name} ... --set {key}=...`)")
    return value


def positive_int_setting(account: Account, key: str, default: int, minimum: int = 1,
                         maximum: Optional[int] = None) -> int:
    raw = account.setting(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise MailError(f"setting {key!r} must be a whole number, got {raw!r}") from None
    if value < minimum or (maximum is not None and value > maximum):
        top = f" and at most {maximum}" if maximum is not None else ""
        raise MailError(f"setting {key!r} must be at least {minimum}{top}, got {value}")
    return value


def choice_setting(account: Account, key: str, choices: Sequence[str], default: str) -> str:
    raw = account.setting(key)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in choices:
        raise MailError(f"setting {key!r} must be one of {', '.join(choices)}; got {raw!r}")
    return value


def limits_from_account(account: Account, archive_default: str = DEFAULT_ARCHIVE_MAILBOX) -> MailLimits:
    return MailLimits(
        max_messages=positive_int_setting(account, "max_messages", DEFAULT_MAX_MESSAGES),
        on_delete=choice_setting(account, "on_delete", DELETE_MODES, DELETE_KEEP),
        archive_mailbox=(account.setting("archive_mailbox") or "").strip() or archive_default,
    )
