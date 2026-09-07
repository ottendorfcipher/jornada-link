"""The IMAP/SMTP backend against imaplib/smtplib look-alikes: listing, fetching, delete modes, sending."""
import pytest

from jornada.sync.accounts import Account
from jornada.sync.mail.backend import MailError, MailLimits, MailSummary
from jornada.sync.mail.imap import BACKEND, build, default_outgoing_host, endpoints
from jornada.sync.mail.imap_client import (Endpoint, ImapBackend, literal_payload, parse_sizes, parse_uids,
                                           password_login, quote_mailbox)
from jornada.sync.registry import BuildContext
from tests.test_mail_fakes import imap_factory, message, smtp_factory

MESSAGES = {"1": message("One", "a"), "2": message("Two", "b"), "3": message("Three", "c")}


def make(limits=MailLimits(), security="ssl", mailbox="INBOX", trash=None, smtp_options=None, **imap_options):
    imaps = imap_factory(MESSAGES, **imap_options)
    smtps = smtp_factory(**(smtp_options or {}))
    backend = ImapBackend("imap", Endpoint("imap.example.org", 993, security), Endpoint("smtp.example.org", 587),
                          mailbox, limits, password_login("me@example.org", "pw"),
                          password_login("me@example.org", "pw"), default_sender="me@example.org",
                          trash_mailbox=trash, imap_factory=imaps, smtp_factory=smtps)
    return backend, imaps, smtps


def test_connect_list_fetch_and_close():
    backend, imaps, _smtps = make(MailLimits(max_messages=2))
    backend.connect()
    client = imaps.created[0]
    assert (client.host, client.port, client.use_ssl) == ("imap.example.org", 993, True)
    assert client.calls[:2] == [("login", "me@example.org", "pw"), ("select", '"INBOX"')]
    assert backend.list() == (MailSummary("2", len(MESSAGES["2"])), MailSummary("3", len(MESSAGES["3"])))
    assert ("uid", "SEARCH", None, "ALL") in client.calls and ("uid", "FETCH", "2,3", "(RFC822.SIZE)") in client.calls
    assert backend.fetch("3") == MESSAGES["3"] and ("uid", "FETCH", "3", "(BODY.PEEK[])") in client.calls
    assert backend.fetch_headers("3") == MESSAGES["3"].split(b"\r\n\r\n")[0] + b"\r\n\r\n"
    assert ("uid", "FETCH", "3", "(BODY.PEEK[HEADER])") in client.calls
    with pytest.raises(MailError):
        backend.fetch("99")
    with pytest.raises(MailError):
        backend.fetch("1;2")
    backend.close()
    assert client.calls[-1] == ("logout",)
    with pytest.raises(MailError):
        backend.list()  # closed
    backend.connect()
    imaps.created[-1].messages.clear()
    assert backend.list() == ()


def test_starttls_login_failure_and_missing_mailbox():
    backend, imaps, _ = make(security="starttls", fail_login=True)
    with pytest.raises(MailError) as info:
        backend.connect()
    assert "Invalid credentials" in str(info.value) and "pw" not in str(info.value)
    assert imaps.created[0].calls[0] == ("starttls",) and imaps.created[0].use_ssl is False
    backend, _, _ = make(mailbox="Nope", missing_mailboxes=("Nope",))
    with pytest.raises(MailError) as info:
        backend.connect()
    assert "Nope" in str(info.value) and "NONEXISTENT" in str(info.value)


def test_delete_modes():
    backend, imaps, _ = make(MailLimits(on_delete="keep"))
    backend.connect()
    backend.delete(("1",))
    assert not any(call[:2] == ("uid", "STORE") for call in imaps.created[0].calls)

    backend, imaps, _ = make(MailLimits(on_delete="delete"))
    backend.connect()
    backend.delete(("1", "2"))
    client = imaps.created[0]
    assert ("uid", "STORE", "1,2", "+FLAGS.SILENT", "(\\Deleted)") in client.calls and ("expunge",) in client.calls
    assert client.expunged == ["1", "2"] and not any(call[:2] == ("uid", "COPY") for call in client.calls)

    backend, imaps, _ = make(MailLimits(on_delete="delete"), trash="[Gmail]/Trash")
    backend.connect()
    backend.delete(("3",))
    assert ("uid", "COPY", "3", '"[Gmail]/Trash"') in imaps.created[0].calls

    backend, imaps, _ = make(MailLimits(on_delete="archive", archive_mailbox="Old Mail"), missing_mailboxes=("Old Mail",))
    backend.connect()
    backend.delete(("2",))
    calls = imaps.created[0].calls
    copy_index = calls.index(("uid", "COPY", "2", '"Old Mail"'))
    assert calls[copy_index + 1] == ("create", '"Old Mail"') and calls[copy_index + 2] == ("uid", "COPY", "2", '"Old Mail"')
    assert calls.index(("expunge",)) > copy_index
    backend.delete(())
    with pytest.raises(MailError):
        backend.delete(("nope",))


def test_send_success_and_failures():
    backend, _, smtps = make()
    backend.connect()
    backend.send(b"raw", "me@example.org", ("bob@example.org",))
    client = smtps.created[0]
    assert (client.host, client.port, client.use_ssl) == ("smtp.example.org", 587, False)
    assert client.calls == [("ehlo",), ("starttls",), ("ehlo",), ("login", "me@example.org", "pw"), ("quit",)]
    assert client.sent == [("me@example.org", ("bob@example.org",), b"raw")]
    backend.send(b"raw", "", ("bob@example.org",))
    assert smtps.created[1].sent[0][0] == "me@example.org"  # envelope sender falls back to the account
    with pytest.raises(MailError):
        backend.send(b"raw", "me@example.org", ())

    backend, _, smtps = make(smtp_options={"refuse": ("bad@example.org",)})
    with pytest.raises(MailError) as info:
        backend.send(b"raw", "me@example.org", ("bad@example.org",))
    assert "refused" in str(info.value) and "bad@example.org" in str(info.value)
    with pytest.raises(MailError):
        backend.send(b"raw", "me@example.org", ("bad@example.org", "ok@example.org"))
    assert smtps.created[-1].calls[-1] == ("quit",)

    backend, _, _ = make(smtp_options={"fail_auth": True})
    with pytest.raises(MailError) as info:
        backend.send(b"raw", "me@example.org", ("bob@example.org",))
    assert "535" in str(info.value) and "pw" not in str(info.value)


def test_build_from_account_settings(tmp_path):
    account = Account("a", "mail", "imap", (("host", "imap.example.org"), ("username", "me@example.org"),
                                            ("security", "starttls"), ("outgoing_security", "ssl"),
                                            ("local_user", "j"), ("max_messages", "5"), ("on_delete", "archive")))
    assert endpoints(account) == (Endpoint("imap.example.org", 143, "starttls"), Endpoint("smtp.example.org", 465, "ssl"))
    assert default_outgoing_host("mail.example.org") == "mail.example.org"
    plain = Account("a", "mail", "imap", (("host", "mail.example.org"), ("username", "me"), ("port", "1993"),
                                          ("outgoing_host", "out.example.org"), ("outgoing_port", "2525"),
                                          ("outgoing_username", "sender")))
    assert endpoints(plain) == (Endpoint("mail.example.org", 1993, "ssl"), Endpoint("out.example.org", 2525, "starttls"))
    imaps, smtps = imap_factory(MESSAGES), smtp_factory()
    ctx = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                       extra={"imap_factory": imaps, "smtp_factory": smtps})
    backend = BACKEND.build(account, {"password": "pw"}, ctx)
    backend.connect()
    assert imaps.created[0].calls[:2] == [("starttls",), ("login", "me@example.org", "pw")]
    assert len(backend.list()) == 3
    backend.send(b"raw", "", ("bob@example.org",))
    assert smtps.created[0].calls[0] == ("ehlo",) and ("login", "me@example.org", "pw") in smtps.created[0].calls
    other = build(plain, {"password": "pw", "outgoing_password": "pw2"}, ctx)
    other.send(b"raw", "", ("bob@example.org",))
    assert ("login", "sender", "pw2") in smtps.created[1].calls and smtps.created[1].sent[0][0] == ""
    with pytest.raises(MailError):
        build(account, {}, ctx)  # no password
    with pytest.raises(MailError):
        endpoints(Account("a", "mail", "imap", (("host", "h"), ("port", "abc"))))
    with pytest.raises(MailError):
        endpoints(Account("a", "mail", "imap", (("host", "h"), ("security", "plain"))))
    with pytest.raises(MailError):
        endpoints(Account("a", "mail", "imap", (("host", "h"), ("port", "70000"))))
    with pytest.raises(MailError):
        endpoints(Account("a", "mail", "imap"))
    assert BACKEND.missing_settings(Account("a", "mail", "imap"), {}) == ("host", "username", "password",
                                                                          "local_user", "local_password")


def test_response_parsers():
    assert parse_uids([b"1 2 3"]) == ("1", "2", "3") and parse_uids([b""]) == () and parse_uids([None]) == ()
    assert parse_sizes([b"1 (UID 7 RFC822.SIZE 100)", b"2 (RFC822.SIZE 5 UID 8)", None]) == {"7": 100, "8": 5}
    assert literal_payload([(b"1 (UID 1 BODY[] {3}", b"abc"), b")"]) == b"abc" and literal_payload([None]) is None
    assert quote_mailbox("[Gmail]/All Mail") == '"[Gmail]/All Mail"' and quote_mailbox('a"b\\c') == '"a\\"b\\\\c"'
