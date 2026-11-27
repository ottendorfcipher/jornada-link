"""Backend ``m365``: Microsoft 365 / Exchange Online over IMAP/SMTP with XOAUTH2."""
from __future__ import annotations

from ...webapi.oauth import Provider, microsoft
from ..accounts import Account
from ..registry import SettingSpec
from .imap_client import SSL, STARTTLS, Endpoint
from .xoauth2 import OAuthMailService, backend_spec

IMAP_HOST = "outlook.office365.com"
SMTP_HOST = "smtp.office365.com"
DEFAULT_TENANT = "common"
SCOPES = ("https://outlook.office.com/IMAP.AccessAsUser.All", "https://outlook.office.com/SMTP.Send",
          "offline_access")


def provider_for(account: Account) -> Provider:
    return microsoft((account.setting("tenant") or "").strip() or DEFAULT_TENANT)


SERVICE = OAuthMailService(
    key="m365",
    title="Microsoft 365 (XOAUTH2)",
    provider_label="Microsoft",
    imap=Endpoint(IMAP_HOST, 993, SSL),
    smtp=Endpoint(SMTP_HOST, 587, STARTTLS),
    scopes=SCOPES,
    provider_for=provider_for,
    archive_default="Archive",
    extra_settings=(SettingSpec("tenant", f"Entra ID tenant id or domain (default {DEFAULT_TENANT})",
                                required=False, default=DEFAULT_TENANT),),
    notes="the app registration needs the IMAP.AccessAsUser.All and SMTP.Send delegated permissions, and "
          "authenticated SMTP must be enabled for the mailbox.",
)

BACKEND = backend_spec(SERVICE)
