"""The bridge's own settings: where it listens, the local login, and the size limits."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Dict

from ..accounts import Account
from ..registry import BuildContext, SettingSpec
from .backend import DEFAULT_MAX_SIZE, MAX_PORT, MailError, choice_setting, positive_int_setting

DEFAULT_LISTEN = "192.168.131.102"
DEFAULT_POP3_PORT = 110
DEFAULT_SMTP_PORT = 25
UNPRIVILEGED_POP3_PORT = 1110
UNPRIVILEGED_SMTP_PORT = 1025
SMTP_MAX_MESSAGE = 10 * 1024 * 1024
MAX_LOGIN_ATTEMPTS = 3
SESSION_TIMEOUT = 600.0
BRIDGE_NAME = "jornada-bridge"

SERVER_SETTINGS = (
    SettingSpec("listen", f"IP address the bridge listens on (default: the Mac's PPP address {DEFAULT_LISTEN})",
                required=False, default=DEFAULT_LISTEN),
    SettingSpec("pop3_port", f"POP3 port the device connects to (default {DEFAULT_POP3_PORT}; ports below 1024 "
                f"need sudo — use {UNPRIVILEGED_POP3_PORT} here and in the device's service settings instead)",
                required=False, default=str(DEFAULT_POP3_PORT)),
    SettingSpec("smtp_port", f"SMTP port the device connects to (default {DEFAULT_SMTP_PORT}; ports below 1024 "
                f"need sudo — use {UNPRIVILEGED_SMTP_PORT} here and on the device instead)",
                required=False, default=str(DEFAULT_SMTP_PORT)),
    SettingSpec("smtp_auth", "require the local user name and password for outgoing mail: on (default) or off "
                "(the bridge only listens on the PPP address, so off is acceptable for an Inbox that cannot "
                "authenticate)", required=False, default="on"),
    SettingSpec("allow_any_interface", "yes to permit listen=0.0.0.0 (exposes the bridge and your mail to the "
                "whole network; default no)", required=False, default="no"),
)
WILDCARD_ADDRESSES = ("", "0.0.0.0", "::", "*")
PRIVATE_LINK_PREFIXES = ("127.", "192.168.131.", "::1")


@dataclass(frozen=True)
class BridgeConfig:
    local_user: str
    local_password: str
    listen: str = DEFAULT_LISTEN
    pop3_port: int = DEFAULT_POP3_PORT
    smtp_port: int = DEFAULT_SMTP_PORT
    max_size: int = DEFAULT_MAX_SIZE
    smtp_auth: bool = True
    smtp_max_message: int = SMTP_MAX_MESSAGE
    max_login_attempts: int = MAX_LOGIN_ATTEMPTS
    session_timeout: float = SESSION_TIMEOUT

    def credentials_match(self, user: str, password: str) -> bool:
        """Constant-time comparison of what the device sent with the local login."""
        user_ok = hmac.compare_digest(user.encode("utf-8"), self.local_user.encode("utf-8"))
        password_ok = hmac.compare_digest(password.encode("utf-8"), self.local_password.encode("utf-8"))
        return user_ok and password_ok


def config_from_account(account: Account, secrets: Dict[str, Any], context: BuildContext) -> BridgeConfig:
    local_user = (account.setting("local_user") or "").strip()
    local_password = str(secrets.get("local_password") or "")
    if not local_user or not local_password:
        raise MailError("the mail bridge needs local_user and local_password (what the device's Inbox logs in "
                        f"with): `jornada sync account add {account.name} ... --set local_user=jornada "
                        "--ask local_password`")
    listen = (account.setting("listen") or "").strip() or context.listen_ip or DEFAULT_LISTEN
    smtp_auth = choice_setting(account, "smtp_auth", ("on", "off"), "on") == "on"
    if listen in WILDCARD_ADDRESSES and choice_setting(account, "allow_any_interface", ("yes", "no"), "no") != "yes":
        raise MailError(f"listen={listen!r} would expose the bridge and your mailbox to every network this Mac is on; "
                        f"use the PPP address ({DEFAULT_LISTEN}) or set allow_any_interface=yes deliberately")
    if not smtp_auth and not listen.startswith(PRIVATE_LINK_PREFIXES):
        raise MailError("smtp_auth=off is only allowed when the bridge listens on the PPP address or loopback "
                        "(anything else would be an open relay through your account)")
    return BridgeConfig(
        local_user=local_user,
        local_password=local_password,
        listen=listen,
        pop3_port=positive_int_setting(account, "pop3_port", DEFAULT_POP3_PORT, minimum=0, maximum=MAX_PORT),
        smtp_port=positive_int_setting(account, "smtp_port", DEFAULT_SMTP_PORT, minimum=0, maximum=MAX_PORT),
        max_size=positive_int_setting(account, "max_size", DEFAULT_MAX_SIZE),
        smtp_auth=smtp_auth,
    )
