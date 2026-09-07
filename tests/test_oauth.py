import threading
import urllib.request

import pytest

from jornada.webapi.http import HttpClient, HttpResponse, fake_transport, json_response
from jornada.webapi.oauth import (GOOGLE, OAuthError, Provider, Token, authorize_url, ensure_fresh, exchange_code, login,
                                  microsoft, pkce_pair, refresh, start_listener, wait_for_code)


def test_pkce_and_authorize_url():
    import base64, hashlib
    verifier, challenge = pkce_pair("a" * 43)
    expected = base64.urlsafe_b64encode(hashlib.sha256(b"a" * 43).digest()).decode().rstrip("=")
    assert verifier == "a" * 43 and challenge == expected and "=" not in challenge
    generated, _ = pkce_pair()
    assert 43 <= len(generated) <= 128
    url = authorize_url(GOOGLE, "cid", "http://127.0.0.1:1/", ("s1", "s2"), "st", challenge)
    assert url.startswith(GOOGLE.auth_url + "?") and "scope=s1+s2" in url and "access_type=offline" in url
    assert microsoft().token_url.endswith("/common/oauth2/v2.0/token") and microsoft("tid").redirect_host == "localhost"


def test_token_helpers():
    token = Token.from_response({"access_token": "a", "refresh_token": "r", "expires_in": 100, "scope": "x"}, now=lambda: 1000.0)
    assert token.expires_at == 1100.0 and token.expires_within(200, now=lambda: 1000.0)
    assert not token.expires_within(50, now=lambda: 1000.0)
    assert Token.from_dict(token.to_dict()) == token
    kept = Token.from_response({"access_token": "b", "expires_in": 10}, previous=token, now=lambda: 0.0)
    assert kept.refresh_token == "r" and kept.scope == "x"
    with pytest.raises(OAuthError):
        Token.from_response({"error": "x"})


def test_exchange_refresh_and_ensure_fresh():
    transport, seen = fake_transport({("POST", "/token"): json_response(200, {"access_token": "new", "refresh_token": "rr", "expires_in": 3600})})
    provider = Provider("p", "https://p/auth", "https://p/token")
    http = HttpClient(transport=transport)
    token = exchange_code(provider, "cid", "sec", "code1", "http://127.0.0.1:5/", "ver", http, now=lambda: 0.0)
    assert token.access_token == "new" and b"code_verifier=ver" in seen[0].body and b"client_secret=sec" in seen[0].body
    old = Token("old", "rtok", 10.0)
    fresh = refresh(provider, "cid", None, old, http, scopes=("s",), now=lambda: 0.0)
    assert fresh.access_token == "new" and b"refresh_token=rtok" in seen[1].body and b"scope=s" in seen[1].body
    assert ensure_fresh(provider, "cid", None, fresh, http, now=lambda: 0.0) is fresh
    assert ensure_fresh(provider, "cid", None, old, http, now=lambda: 0.0).access_token == "new"
    with pytest.raises(OAuthError):
        refresh(provider, "cid", None, Token("x", None, 0), http)
    failing, _ = fake_transport({("POST", "/token"): HttpResponse(400, (), b'{"error":"invalid_grant"}')})
    with pytest.raises(OAuthError) as info:
        exchange_code(provider, "cid", None, "c", "u", "v", HttpClient(transport=failing))
    assert "400" in str(info.value)


def _hit(port: int, query: str) -> None:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/?{query}", timeout=5).read()


def test_listener_captures_code_and_rejects_bad_state():
    server, capture = start_listener("state1")
    port = server.server_address[1]
    threading.Thread(target=_hit, args=(port, "state=state1&code=abc"), daemon=True).start()
    assert wait_for_code(server, capture, timeout=5) == "abc"
    server, capture = start_listener("state2")
    threading.Thread(target=_hit, args=(server.server_address[1], "state=wrong&code=abc"), daemon=True).start()
    with pytest.raises(OAuthError):
        wait_for_code(server, capture, timeout=5)
    server, capture = start_listener("state3")
    threading.Thread(target=_hit, args=(server.server_address[1], "state=state3&error=access_denied"), daemon=True).start()
    with pytest.raises(OAuthError):
        wait_for_code(server, capture, timeout=5)
    server, capture = start_listener("state4")
    with pytest.raises(OAuthError):
        wait_for_code(server, capture, timeout=0.2)


def test_full_login_flow_with_fake_browser():
    transport, seen = fake_transport({("POST", "/token"): json_response(200, {"access_token": "tok", "expires_in": 60})})
    provider = Provider("p", "https://p/auth", "https://p/token")
    announced = []

    def browser(url: str) -> None:
        from urllib.parse import parse_qs, urlsplit
        params = parse_qs(urlsplit(url).query)
        redirect = params["redirect_uri"][0]
        threading.Thread(target=lambda: urllib.request.urlopen(f"{redirect}?state={params['state'][0]}&code=zzz", timeout=5).read(),
                         daemon=True).start()

    token = login(provider, "cid", None, ("s",), HttpClient(transport=transport), open_browser=browser, timeout=5,
                  announce=announced.append)
    assert token.access_token == "tok" and b"code=zzz" in seen[0].body and announced and "https://p/auth" in announced[0]
