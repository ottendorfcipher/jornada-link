"""Backend ``imap``: any mailbox reachable over IMAP with a password, sending through SMTP."""
from __future__ import annotations

from typing import Any, Dict, Tuple

from ..accounts import Account
from ..registry import BackendSpec, BuildContext, SettingSpec
from .backend import MAX_PORT, MailError, choice_setting, limits_from_account, positive_int_setting, \
    required_setting, shared_settings
from .imap_client import (DEFAULT_MAILBOX, SECURITIES, SSL, STARTTLS, Endpoint, ImapBackend, factories_from_context,
                          password_login)

IMAP_SSL_PORT = 993
IMAP_STARTTLS_PORT = 143
SMTP_SSL_PORT = 465
SMTP_STARTTLS_PORT = 587

SETTINGS = (
    SettingSpec("host", "IMAP server, e.g. imap.example.org"),
    SettingSpec("port", f"IMAP port (default {IMAP_SSL_PORT} for ssl, {IMAP_STARTTLS_PORT} for starttls)",
                required=False),
    SettingSpec("security", "how IMAP is encrypted: ssl (default) or starttls", required=False, default=SSL),
    SettingSpec("username", "IMAP user name (usually the e-mail address)"),
    SettingSpec("password", "IMAP password (an app password where the provider issues them)", secret=True),
    SettingSpec("mailbox", f"mailbox offered to the device (default {DEFAULT_MAILBOX})", required=False,
                default=DEFAULT_MAILBOX),
    SettingSpec("outgoing_host", "SMTP server for sending (default: the IMAP host, imap. replaced by smtp.)",
                required=False),
    SettingSpec("outgoing_port", f"SMTP port (default {SMTP_STARTTLS_PORT} for starttls, {SMTP_SSL_PORT} for ssl)",
                required=False),
    SettingSpec("outgoing_security", "how SMTP is encrypted: starttls (default) or ssl", required=False,
                default=STARTTLS),
    SettingSpec("outgoing_username", "SMTP user name when it differs from username", required=False),
    SettingSpec("outgoing_password", "SMTP password when it differs from password", required=False, secret=True),
) + shared_settings()


def default_outgoing_host(host: str) -> str:
    return "smtp." + host[len("imap."):] if host.lower().startswith("imap.") else host


def endpoints(account: Account) -> Tuple[Endpoint, Endpoint]:
    host = required_setting(account, "host")
    security = choice_setting(account, "security", SECURITIES, SSL)
    imap_port = positive_int_setting(account, "port", IMAP_SSL_PORT if security == SSL else IMAP_STARTTLS_PORT,
                                     maximum=MAX_PORT)
    out_security = choice_setting(account, "outgoing_security", SECURITIES, STARTTLS)
    out_host = (account.setting("outgoing_host") or "").strip() or default_outgoing_host(host)
    out_port = positive_int_setting(account, "outgoing_port",
                                    SMTP_SSL_PORT if out_security == SSL else SMTP_STARTTLS_PORT, maximum=MAX_PORT)
    return Endpoint(host, imap_port, security), Endpoint(out_host, out_port, out_security)


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> ImapBackend:
    imap, smtp = endpoints(account)
    username = required_setting(account, "username")
    password = str(secrets.get("password") or "")
    if not password:
        raise MailError(f"account {account.name!r} needs the secret setting 'password' "
                        f"(`jornada sync account add {account.name} ... --ask password`)")
    outgoing_user = (account.setting("outgoing_username") or "").strip() or username
    outgoing_password = str(secrets.get("outgoing_password") or "") or password
    imap_factory, smtp_factory = factories_from_context(context)
    return ImapBackend(
        "imap", imap, smtp,
        mailbox=(account.setting("mailbox") or "").strip() or DEFAULT_MAILBOX,
        limits=limits_from_account(account),
        imap_login=password_login(username, password),
        smtp_login=password_login(outgoing_user, outgoing_password),
        default_sender=username if "@" in username else "",
        imap_factory=imap_factory, smtp_factory=smtp_factory,
    )


BACKEND = BackendSpec(
    "imap", "Any IMAP account (password)", SETTINGS, build,
    notes="TLS only (IMAP over SSL or STARTTLS, SMTP STARTTLS or SSL) with certificate verification; "
          "providers that require OAuth are covered by the gmail and m365 backends.",
)
