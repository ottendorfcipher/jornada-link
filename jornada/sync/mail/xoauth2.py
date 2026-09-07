"""XOAUTH2 (an OAuth bearer token as the IMAP/SMTP password) and the backend builder that
Gmail and Microsoft 365 share; only hosts, provider and scopes differ between them."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ...webapi.oauth import Provider
from ...webapi.oauth_accounts import authorization_provider, login_account, oauth_settings
from ..accounts import Account
from ..registry import BackendSpec, BuildContext, SettingSpec
from .backend import limits_from_account, required_setting, shared_settings
from .imap_client import DEFAULT_MAILBOX, Authenticator, Endpoint, ImapBackend, factories_from_context

BEARER = "Bearer "
AuthorizationProvider = Callable[[], Optional[str]]


def xoauth2_string(user: str, authorization: Optional[str]) -> str:
    """The SASL XOAUTH2 client response: ``user=…\\x01auth=Bearer …\\x01\\x01``."""
    token = authorization or ""
    header = token if token.lower().startswith(BEARER.lower()) else BEARER + token
    return f"user={user}\x01auth={header}\x01\x01"


def imap_xoauth2_login(user: str, authorization: AuthorizationProvider) -> Authenticator:
    """imaplib sends the response to the first (empty) challenge and cancels on an error challenge."""

    def login(client: Any) -> None:
        initial = xoauth2_string(user, authorization()).encode("utf-8")

        def respond(challenge: Any) -> Optional[bytes]:
            return initial if not challenge else None

        client.authenticate("XOAUTH2", respond)

    return login


def smtp_xoauth2_login(user: str, authorization: AuthorizationProvider) -> Authenticator:
    """smtplib sends the initial response with AUTH; an error challenge gets an empty line back."""

    def login(client: Any) -> None:
        initial = xoauth2_string(user, authorization())

        def respond(challenge: Any = None) -> str:
            return initial if challenge is None else ""

        client.auth("XOAUTH2", respond, initial_response_ok=True)

    return login


@dataclass(frozen=True)
class OAuthMailService:
    """A provider whose IMAP/SMTP take XOAUTH2: what gmail.py and m365.py declare."""

    key: str
    title: str
    provider_label: str
    imap: Endpoint
    smtp: Endpoint
    scopes: Tuple[str, ...]
    provider_for: Callable[[Account], Provider]
    archive_default: str
    trash_mailbox: Optional[str] = None
    extra_settings: Tuple[SettingSpec, ...] = ()
    notes: str = ""

    def settings(self) -> Tuple[SettingSpec, ...]:
        return oauth_settings(self.provider_label) + (
            SettingSpec("email", f"the {self.provider_label} address (also the XOAUTH2 user)"),
            SettingSpec("mailbox", f"mailbox offered to the device (default {DEFAULT_MAILBOX})", required=False,
                        default=DEFAULT_MAILBOX),
        ) + self.extra_settings + shared_settings(self.archive_default)


def build_oauth_backend(service: OAuthMailService, account: Account, secrets: Dict[str, Any],
                        context: BuildContext) -> ImapBackend:
    email = required_setting(account, "email")
    provider = service.provider_for(account)
    authorization = authorization_provider(provider, service.scopes, account, secrets, context)
    imap_factory, smtp_factory = factories_from_context(context)
    return ImapBackend(
        service.key, service.imap, service.smtp,
        mailbox=(account.setting("mailbox") or "").strip() or DEFAULT_MAILBOX,
        limits=limits_from_account(account, service.archive_default),
        imap_login=imap_xoauth2_login(email, authorization),
        smtp_login=smtp_xoauth2_login(email, authorization),
        default_sender=email, trash_mailbox=service.trash_mailbox,
        imap_factory=imap_factory, smtp_factory=smtp_factory,
    )


def backend_spec(service: OAuthMailService) -> BackendSpec:
    def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> ImapBackend:
        return build_oauth_backend(service, account, secrets, context)

    def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
        return login_account(service.provider_for(account), service.scopes, account, secrets, context)

    return BackendSpec(service.key, service.title, service.settings(), build, login=login, notes=service.notes)
