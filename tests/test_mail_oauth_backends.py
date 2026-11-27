"""Gmail and Microsoft 365: XOAUTH2 strings, hosts, scopes and token refresh, without a network."""
import time

import pytest

from jornada.sync.accounts import Account, AccountError
from jornada.sync.mail import MODULE, gmail, m365
from jornada.sync.mail.xoauth2 import imap_xoauth2_login, smtp_xoauth2_login, xoauth2_string
from jornada.sync.registry import BuildContext
from jornada.webapi.http import fake_transport, json_response
from jornada.webapi.oauth import Token
from jornada.webapi.oauth_accounts import TOKEN_KEY
from tests.test_mail_fakes import imap_factory, message, smtp_factory

MESSAGES = {"1": message("One", "a"), "2": message("Two", "b")}
XOAUTH2 = "user=me@gmail.com\x01auth=Bearer tok\x01\x01"


def context(tmp_path, imaps, smtps, transport=None, saved=None):
    return BuildContext(log=lambda _l: None, sync_dir=tmp_path, http_transport=transport,
                        save_secrets=(saved if saved is not None else []).append,
                        extra={"imap_factory": imaps, "smtp_factory": smtps})


def token(expires_in=3600.0):
    return {TOKEN_KEY: Token("tok", "rt", time.time() + expires_in).to_dict()}


def test_xoauth2_string_and_login_callables():
    assert xoauth2_string("me@gmail.com", "Bearer tok") == XOAUTH2
    assert xoauth2_string("me@gmail.com", "tok") == XOAUTH2 and xoauth2_string("u", None) == "user=u\x01auth=Bearer \x01\x01"

    class Imap:
        def authenticate(self, mechanism, authobject):
            self.mechanism, self.first, self.second = mechanism, authobject(b""), authobject(b"eyJzdGF0dXMiOiI0MDEifQ==")

    class Smtp:
        def auth(self, mechanism, authobject, initial_response_ok=True):
            self.mechanism, self.first, self.second, self.initial = mechanism, authobject(), authobject(b"c"), initial_response_ok

    imap, smtp = Imap(), Smtp()
    imap_xoauth2_login("me@gmail.com", lambda: "Bearer tok")(imap)
    assert (imap.mechanism, imap.first, imap.second) == ("XOAUTH2", XOAUTH2.encode("utf-8"), None)
    smtp_xoauth2_login("me@gmail.com", lambda: "Bearer tok")(smtp)
    assert (smtp.mechanism, smtp.first, smtp.second, smtp.initial) == ("XOAUTH2", XOAUTH2, "", True)


def test_gmail_backend_talks_xoauth2_to_google(tmp_path):
    account = Account("g", "mail", "gmail", (("client_id", "cid"), ("email", "me@gmail.com"), ("local_user", "j"),
                                             ("on_delete", "delete")))
    imaps, smtps = imap_factory(MESSAGES), smtp_factory()
    backend = gmail.BACKEND.build(account, token(), context(tmp_path, imaps, smtps))
    assert backend.name == "gmail"
    backend.connect()
    client = imaps.created[0]
    assert (client.host, client.port, client.use_ssl) == ("imap.gmail.com", 993, True)
    assert client.calls[:2] == [("authenticate", "XOAUTH2", XOAUTH2.encode("utf-8")), ("select", '"INBOX"')]
    assert len(backend.list()) == 2
    backend.delete(("1",))
    assert ("uid", "COPY", "1", '"[Gmail]/Trash"') in client.calls and client.expunged == ["1"]
    backend.send(b"raw", "", ("bob@example.org",))
    smtp = smtps.created[0]
    assert (smtp.host, smtp.port, smtp.use_ssl) == ("smtp.gmail.com", 587, False)
    assert smtp.calls[:3] == [("ehlo",), ("starttls",), ("ehlo",)] and ("auth", "XOAUTH2", XOAUTH2, "") in smtp.calls
    assert smtp.sent == [("me@gmail.com", ("bob@example.org",), b"raw")]
    assert gmail.SERVICE.scopes == ("https://mail.google.com/",) and gmail.provider_for(account).name == "google"
    archive = Account("g", "mail", "gmail", (("client_id", "cid"), ("email", "me@gmail.com"), ("on_delete", "archive")))
    imaps = imap_factory(MESSAGES)
    backend = gmail.BACKEND.build(archive, token(), context(tmp_path, imaps, smtp_factory()))
    backend.connect()
    backend.delete(("2",))
    assert ("uid", "COPY", "2", '"[Gmail]/All Mail"') in imaps.created[0].calls


def test_gmail_refreshes_an_expired_token_and_saves_it(tmp_path):
    transport, seen = fake_transport({("POST", "/token"): json_response(200, {"access_token": "new", "expires_in": 3600})})
    saved = []
    account = Account("g", "mail", "gmail", (("client_id", "cid"), ("email", "me@gmail.com")))
    imaps = imap_factory(MESSAGES)
    backend = gmail.BACKEND.build(account, {**token(-10), "client_secret": "sec"},
                                  context(tmp_path, imaps, smtp_factory(), transport, saved))
    backend.connect()
    assert imaps.created[0].calls[0] == ("authenticate", "XOAUTH2", b"user=me@gmail.com\x01auth=Bearer new\x01\x01")
    assert len(seen) == 1 and saved and saved[0][TOKEN_KEY]["access_token"] == "new"
    failing, _ = fake_transport({})
    backend = gmail.BACKEND.build(account, token(-10), context(tmp_path, imap_factory(MESSAGES), smtp_factory(), failing))
    with pytest.raises(Exception) as info:
        backend.connect()
    assert "sign in" in str(info.value).lower() or "sign-in" in str(info.value).lower()


def test_m365_backend_uses_tenant_and_office_hosts(tmp_path):
    account = Account("o", "mail", "m365", (("client_id", "cid"), ("email", "me@contoso.com"),
                                            ("tenant", "contoso.onmicrosoft.com"), ("on_delete", "archive")))
    assert "contoso.onmicrosoft.com" in m365.provider_for(account).token_url
    assert "/common/" in m365.provider_for(Account("o", "mail", "m365")).token_url
    assert m365.SERVICE.scopes == ("https://outlook.office.com/IMAP.AccessAsUser.All",
                                   "https://outlook.office.com/SMTP.Send", "offline_access")
    imaps, smtps = imap_factory(MESSAGES), smtp_factory()
    backend = m365.BACKEND.build(account, token(), context(tmp_path, imaps, smtps))
    backend.connect()
    client = imaps.created[0]
    assert (client.host, client.port) == ("outlook.office365.com", 993)
    assert client.calls[0] == ("authenticate", "XOAUTH2", b"user=me@contoso.com\x01auth=Bearer tok\x01\x01")
    backend.delete(("2",))
    assert ("uid", "COPY", "2", '"Archive"') in client.calls
    backend.send(b"raw", "me@contoso.com", ("bob@example.org",))
    assert (smtps.created[0].host, smtps.created[0].port) == ("smtp.office365.com", 587)


def test_oauth_backends_need_sign_in_email_and_client_id(tmp_path):
    ctx = context(tmp_path, imap_factory(MESSAGES), smtp_factory())
    signed_out = Account("g", "mail", "gmail", (("client_id", "cid"), ("email", "me@gmail.com")))
    with pytest.raises(AccountError):
        gmail.BACKEND.build(signed_out, {}, ctx)
    with pytest.raises(AccountError):
        gmail.BACKEND.login(Account("g", "mail", "gmail"), {}, ctx)  # client_id first
    with pytest.raises(Exception):
        gmail.BACKEND.build(Account("g", "mail", "gmail", (("client_id", "cid"),)), token(), ctx)  # email missing
    assert gmail.BACKEND.missing_settings(Account("g", "mail", "gmail"), {}) == ("client_id", "token", "email",
                                                                                 "local_user", "local_password")
    assert "tenant" in [s.key for s in MODULE.backend("m365").settings]
    assert MODULE.backend("gmail").notes and MODULE.backend("m365").login is not None
