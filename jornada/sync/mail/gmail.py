"""Backend ``gmail``: Gmail over IMAP/SMTP with XOAUTH2 (the user's own Google OAuth client)."""
from __future__ import annotations

from ...webapi.oauth import GOOGLE
from ..accounts import Account
from .imap_client import SSL, STARTTLS, Endpoint
from .xoauth2 import OAuthMailService, backend_spec

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SCOPES = ("https://mail.google.com/",)
ALL_MAIL = "[Gmail]/All Mail"
TRASH = "[Gmail]/Trash"

SERVICE = OAuthMailService(
    key="gmail",
    title="Gmail (XOAUTH2)",
    provider_label="Google",
    imap=Endpoint(IMAP_HOST, 993, SSL),
    smtp=Endpoint(SMTP_HOST, 587, STARTTLS),
    scopes=SCOPES,
    provider_for=lambda _account: GOOGLE,
    archive_default=ALL_MAIL,
    trash_mailbox=TRASH,
    notes="enable IMAP in Gmail settings; the OAuth client needs the https://mail.google.com/ scope. "
          f"on_delete=archive removes the Inbox label (copy to {ALL_MAIL}), on_delete=delete moves to {TRASH}.",
)


def provider_for(account: Account):
    return SERVICE.provider_for(account)


BACKEND = backend_spec(SERVICE)
