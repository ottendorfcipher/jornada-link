"""OAuth for sync accounts: the settings a backend declares, the sign-in step, and an
``Authorization`` provider that refreshes and persists the token as needed.

The user brings their own OAuth client (a Google Cloud "Desktop app" client or an
Azure public-client app registration); its id is a plain setting, its secret (Google
issues one even for desktop apps) is stored with the secrets.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple

from ..sync.accounts import Account, AccountError
from ..sync.registry import BuildContext, SettingSpec
from .http import HttpClient, bearer
from .oauth import OAuthError, Provider, Token, ensure_fresh, login

TOKEN_KEY = "token"


def oauth_settings(provider_label: str) -> Tuple[SettingSpec, ...]:
    return (
        SettingSpec("client_id", f"OAuth client id of your own {provider_label} app registration"),
        SettingSpec("client_secret", f"OAuth client secret ({provider_label} desktop clients have one)",
                    required=False, secret=True),
        SettingSpec(TOKEN_KEY, "sign-in token (filled by `jornada sync account login`)", secret=True),
    )


def login_account(provider: Provider, scopes: Tuple[str, ...], account: Account, secrets: Dict[str, Any],
                  context: BuildContext) -> Dict[str, Any]:
    """The ``login`` step of a backend: browser sign-in; returns the secrets to store."""
    client_id = account.setting("client_id")
    if not client_id:
        raise AccountError("set client_id first: `jornada sync account add ... --set client_id=...`")
    http = HttpClient(transport=context.http_transport)
    token = login(provider, client_id, secrets.get("client_secret"), scopes, http,
                  open_browser=context.open_browser, announce=context.log)
    return {TOKEN_KEY: token.to_dict()}


def token_from_secrets(secrets: Dict[str, Any]) -> Token:
    raw = secrets.get(TOKEN_KEY)
    if not isinstance(raw, dict):
        raise AccountError("this account is not signed in yet; run `jornada sync account login NAME`")
    try:
        return Token.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise AccountError("the stored sign-in token is unreadable; sign in again") from exc


def authorization_provider(provider: Provider, scopes: Tuple[str, ...], account: Account,
                           secrets: Dict[str, Any], context: BuildContext,
                           now: Callable[[], float] = time.time) -> Callable[[], Optional[str]]:
    """An ``auth`` callable for :class:`HttpClient` that keeps the token fresh and saved."""
    state = {"token": token_from_secrets(secrets)}
    client_id = account.setting("client_id") or ""
    client_secret = secrets.get("client_secret")
    http = HttpClient(transport=context.http_transport)

    def authorization() -> Optional[str]:
        current = state["token"]
        try:
            fresh = ensure_fresh(provider, client_id, client_secret, current, http, scopes, now=now)
        except OAuthError as exc:
            raise AccountError(f"{provider.name} sign-in expired ({exc}); run `jornada sync account login {account.name}`") from exc
        if fresh is not current:
            state["token"] = fresh
            context.save_secrets({TOKEN_KEY: fresh.to_dict()})
        return bearer(fresh.access_token)

    return authorization


def api_client(base_url: str, provider: Provider, scopes: Tuple[str, ...], account: Account,
               secrets: Dict[str, Any], context: BuildContext) -> HttpClient:
    """An HttpClient for ``base_url`` authenticated with the account's OAuth token."""
    return HttpClient(base_url, transport=context.http_transport,
                      auth=authorization_provider(provider, scopes, account, secrets, context))
