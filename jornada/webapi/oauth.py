"""OAuth 2.0 authorization-code flow with PKCE for desktop use.

The browser is sent to the provider; the provider redirects back to a one-shot
HTTP listener on the loopback interface which captures the code. Tokens are
returned as values — persisting them is the account store's job.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

from .http import HttpClient, HttpError


class OAuthError(RuntimeError):
    """The flow could not complete (message is safe to show; contains no secrets)."""


@dataclass(frozen=True)
class Provider:
    name: str
    auth_url: str
    token_url: str
    redirect_host: str = "127.0.0.1"
    extra_auth_params: Tuple[Tuple[str, str], ...] = ()


GOOGLE = Provider(
    name="google",
    auth_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    extra_auth_params=(("access_type", "offline"), ("prompt", "consent")),
)


def microsoft(tenant: str = "common") -> Provider:
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return Provider(name="microsoft", auth_url=f"{base}/authorize", token_url=f"{base}/token",
                    redirect_host="localhost")


@dataclass(frozen=True)
class Token:
    access_token: str
    refresh_token: Optional[str]
    expires_at: float
    scope: str = ""
    token_type: str = "Bearer"

    def expires_within(self, seconds: float, now: Callable[[], float] = time.time) -> bool:
        return self.expires_at - now() < seconds

    def to_dict(self) -> Dict[str, Any]:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": self.expires_at, "scope": self.scope, "token_type": self.token_type}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Token":
        return cls(str(data["access_token"]), data.get("refresh_token"), float(data.get("expires_at", 0)),
                   str(data.get("scope", "")), str(data.get("token_type", "Bearer")))

    @classmethod
    def from_response(cls, payload: Dict[str, Any], previous: Optional["Token"] = None,
                      now: Callable[[], float] = time.time) -> "Token":
        if "access_token" not in payload:
            raise OAuthError("token response has no access_token")
        refresh = payload.get("refresh_token") or (previous.refresh_token if previous else None)
        return cls(str(payload["access_token"]), refresh, now() + float(payload.get("expires_in", 3600)),
                   str(payload.get("scope", previous.scope if previous else "")),
                   str(payload.get("token_type", "Bearer")))


def pkce_pair(verifier: Optional[str] = None) -> Tuple[str, str]:
    verifier = verifier or secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_url(provider: Provider, client_id: str, redirect_uri: str, scopes: Tuple[str, ...],
                  state: str, challenge: str) -> str:
    params = [("client_id", client_id), ("redirect_uri", redirect_uri), ("response_type", "code"),
              ("scope", " ".join(scopes)), ("state", state), ("code_challenge", challenge),
              ("code_challenge_method", "S256"), *provider.extra_auth_params]
    return provider.auth_url + "?" + urllib.parse.urlencode(params)


class _Capture:
    def __init__(self, state: str) -> None:
        self.state = state
        self.code: Optional[str] = None
        self.error: Optional[str] = None
        self.done = threading.Event()


class _RedirectHandler(BaseHTTPRequestHandler):
    capture: _Capture

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        capture = self.capture
        if query.get("state", [None])[0] != capture.state:
            capture.error = "state mismatch (possible cross-site request); try again"
        elif "error" in query:
            capture.error = query.get("error_description", query["error"])[0]
        else:
            capture.code = query.get("code", [None])[0]
            if capture.code is None:
                capture.error = "no authorization code in the redirect"
        message = "You can close this window." if capture.code else f"Sign-in failed: {capture.error}"
        body = f"<html><body><h2>Jornada Sync</h2><p>{message}</p></body></html>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        capture.done.set()

    def log_message(self, *_args: Any) -> None:
        return


def start_listener(state: str, port: int = 0) -> Tuple[HTTPServer, _Capture]:
    capture = _Capture(state)
    handler = type("Handler", (_RedirectHandler,), {"capture": capture})
    server = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, capture


def wait_for_code(server: HTTPServer, capture: _Capture, timeout: float) -> str:
    try:
        if not capture.done.wait(timeout):
            raise OAuthError("timed out waiting for the browser sign-in")
    finally:
        server.shutdown()
        server.server_close()
    if capture.error or capture.code is None:
        raise OAuthError(capture.error or "sign-in did not return a code")
    return capture.code


def _token_request(provider: Provider, http: HttpClient, form: Dict[str, str], previous: Optional[Token],
                   now: Callable[[], float]) -> Token:
    try:
        response = http.post(provider.token_url, form=form, headers=(("Accept", "application/json"),))
    except HttpError as exc:
        raise OAuthError(f"{provider.name} token request failed ({exc.status})") from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise OAuthError(f"{provider.name} token response is not an object")
    return Token.from_response(payload, previous, now)


def exchange_code(provider: Provider, client_id: str, client_secret: Optional[str], code: str,
                  redirect_uri: str, verifier: str, http: HttpClient,
                  now: Callable[[], float] = time.time) -> Token:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": verifier}
    if client_secret:
        form["client_secret"] = client_secret
    return _token_request(provider, http, form, None, now)


def refresh(provider: Provider, client_id: str, client_secret: Optional[str], token: Token,
            http: HttpClient, scopes: Tuple[str, ...] = (), now: Callable[[], float] = time.time) -> Token:
    if not token.refresh_token:
        raise OAuthError(f"the {provider.name} token cannot be refreshed; sign in again")
    form = {"grant_type": "refresh_token", "refresh_token": token.refresh_token, "client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    if scopes:
        form["scope"] = " ".join(scopes)
    return _token_request(provider, http, form, token, now)


def ensure_fresh(provider: Provider, client_id: str, client_secret: Optional[str], token: Token,
                 http: HttpClient, scopes: Tuple[str, ...] = (), margin: float = 60.0,
                 now: Callable[[], float] = time.time) -> Token:
    if not token.expires_within(margin, now):
        return token
    return refresh(provider, client_id, client_secret, token, http, scopes, now)


def login(provider: Provider, client_id: str, client_secret: Optional[str], scopes: Tuple[str, ...],
          http: HttpClient, open_browser: Callable[[str], Any] = webbrowser.open, listen_port: int = 0,
          timeout: float = 300.0, now: Callable[[], float] = time.time,
          announce: Callable[[str], None] = lambda _line: None) -> Token:
    """Run the whole browser flow and return a token."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    server, capture = start_listener(state, listen_port)
    redirect_uri = f"http://{provider.redirect_host}:{server.server_address[1]}/"
    url = authorize_url(provider, client_id, redirect_uri, scopes, state, challenge)
    announce(f"opening the browser for {provider.name} sign-in; if nothing opens, visit:\n{url}")
    open_browser(url)
    code = wait_for_code(server, capture, timeout)
    return exchange_code(provider, client_id, client_secret, code, redirect_uri, verifier, http, now)


def with_expiry(token: Token, expires_at: float) -> Token:
    return replace(token, expires_at=expires_at)
