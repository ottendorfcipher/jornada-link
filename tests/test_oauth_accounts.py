from pathlib import Path

import pytest

from jornada.sync.accounts import Account, AccountError
from jornada.sync.registry import BuildContext
from jornada.webapi.http import HttpClient, fake_transport, json_response
from jornada.webapi.oauth import Provider, Token
from jornada.webapi.oauth_accounts import (TOKEN_KEY, api_client, authorization_provider, login_account,
                                           oauth_settings, token_from_secrets)

PROVIDER = Provider("p", "https://p/auth", "https://p/token")


def context(transport, saved, tmp_path):
    return BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=saved.append, http_transport=transport)


def test_settings_and_token_parsing():
    keys = [s.key for s in oauth_settings("Google")]
    assert keys == ["client_id", "client_secret", TOKEN_KEY]
    assert [s.secret for s in oauth_settings("x")] == [False, True, True]
    with pytest.raises(AccountError):
        token_from_secrets({})
    with pytest.raises(AccountError):
        token_from_secrets({TOKEN_KEY: {"nope": 1}})
    assert token_from_secrets({TOKEN_KEY: Token("a", "r", 5.0).to_dict()}).access_token == "a"


def test_authorization_refreshes_and_persists(tmp_path):
    transport, seen = fake_transport({
        ("POST", "/token"): json_response(200, {"access_token": "new", "expires_in": 3600}),
        ("GET", "/me"): json_response(200, {"id": 1}),
    })
    saved = []
    account = Account("a", "calendar", "google", (("client_id", "cid"),))
    secrets = {TOKEN_KEY: Token("old", "rt", 100.0).to_dict(), "client_secret": "sec"}
    auth = authorization_provider(PROVIDER, ("s",), account, secrets, context(transport, saved, tmp_path), now=lambda: 90.0)
    assert auth() == "Bearer new" and saved == [{TOKEN_KEY: {"access_token": "new", "refresh_token": "rt", "expires_at": 3690.0, "scope": "", "token_type": "Bearer"}}]
    assert auth() == "Bearer new" and len(saved) == 1 and len(seen) == 1
    client = api_client("https://api", PROVIDER, ("s",), account, secrets, context(transport, saved, tmp_path))
    assert client.get_json("/me") == {"id": 1}
    with pytest.raises(AccountError):
        authorization_provider(PROVIDER, (), account, {TOKEN_KEY: Token("x", None, 0).to_dict()},
                               context(transport, saved, tmp_path), now=lambda: 10.0)()


def test_login_requires_client_id_and_stores_token(tmp_path):
    saved = []
    with pytest.raises(AccountError):
        login_account(PROVIDER, (), Account("a", "m", "b"), {}, context(None, saved, tmp_path))
    import threading, urllib.request
    from urllib.parse import parse_qs, urlsplit
    transport, _ = fake_transport({("POST", "/token"): json_response(200, {"access_token": "t", "expires_in": 5})})

    def browser(url):
        params = parse_qs(urlsplit(url).query)
        threading.Thread(target=lambda: urllib.request.urlopen(
            f"{params['redirect_uri'][0]}?state={params['state'][0]}&code=c", timeout=5).read(), daemon=True).start()

    ctx = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=saved.append, http_transport=transport,
                       open_browser=browser)
    result = login_account(PROVIDER, ("s",), Account("a", "m", "b", (("client_id", "cid"),)), {}, ctx)
    assert result[TOKEN_KEY]["access_token"] == "t"
