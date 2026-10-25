"""The Mail.app backend with a fake osascript runner: mailbox check, listing, delete modes, sending."""
import email
import email.policy

import pytest

from jornada.sync.accounts import Account
from jornada.sync.mail import MODULE
from jornada.sync.mail.apple import BACKEND, AppleMailBackend, build_message, parse_js_date, text_of
from jornada.sync.mail.backend import MailError, MailLimits
from jornada.sync.registry import BuildContext
from jornada.webapi.applescript import fake_runner

ITEMS = [
    {"id": 501, "subject": "Hello", "sender": "Alice Example <alice@example.org>",
     "to": [{"name": "Bob", "address": "bob@example.org"}], "cc": [], "date": "2026-09-06T10:00:00.000Z",
     "messageId": "abc@example.org", "content": "Hi Bob\n.leading dot\n"},
    {"id": 502, "subject": "Zweite Nachricht", "sender": "carol@example.org", "to": [],
     "cc": [{"name": "", "address": "dave@example.org"}], "date": None, "messageId": "<def@example.org>",
     "content": "Grüße"},
    {"id": 503, "error": "can't get message"},
]


def backend(answers, calls, account="Work", sender="", limits=MailLimits()):
    return AppleMailBackend(account, "INBOX", sender, limits, runner=fake_runner(answers, calls))


def test_connect_checks_the_mailbox():
    calls = []
    mail = backend([{"mailboxes": 1}, {"mailboxes": 0}, RuntimeError("Mail got an error: not allowed")], calls)
    mail.connect()
    argv, script, _timeout = calls[0]
    assert argv[:4] == ("/usr/bin/osascript", "-l", "JavaScript", "-") and argv[4:] == ("Work", "INBOX")
    assert "function locate(" in script and "JSON.stringify" in script
    with pytest.raises(MailError) as info:
        mail.connect()
    assert "INBOX" in str(info.value) and "Work" in str(info.value)
    with pytest.raises(MailError) as info:
        mail.connect()
    assert "not allowed" in str(info.value)
    mail.close()


def test_list_builds_rfc2822_messages_and_caches_them():
    calls = []
    mail = backend([ITEMS], calls, limits=MailLimits(max_messages=7))
    summaries = mail.list()
    assert calls[0][0][4:] == ("Work", "INBOX", "7")
    assert [s.uid for s in summaries] == ["501", "502"]  # the message Mail could not read is skipped
    raw = mail.fetch("501")
    assert summaries[0].size == len(raw)
    for expected in (b"From: Alice Example <alice@example.org>", b"To: Bob <bob@example.org>", b"Subject: Hello",
                     b"Message-ID: <abc@example.org>", b"Date: Sun, 06 Sep 2026 10:00:00 +0000"):
        assert expected in raw
    assert raw.endswith(b"\r\n\r\nHi Bob\r\n.leading dot\r\n") and b"\n" not in raw.replace(b"\r\n", b"")
    headers = mail.fetch_headers("501")
    assert headers.startswith(b"From:") and b"Hi Bob" not in headers
    parsed = email.message_from_bytes(mail.fetch("502"), policy=email.policy.default)
    assert parsed["Subject"] == "Zweite Nachricht" and parsed["Cc"] == "dave@example.org"
    assert parsed["To"] == "undisclosed-recipients:;" and parsed["Message-ID"] == "<def@example.org>"
    assert parsed.get_content().replace("\r\n", "\n") == "Grüße\n" and parsed["Date"]
    assert len(calls) == 1  # fetches come from the cache
    with pytest.raises(MailError):
        mail.fetch("999")  # a refresh finds nothing (no canned answer left)


def test_delete_modes_and_errors():
    calls = []
    limits = MailLimits(on_delete="archive", archive_mailbox="Old")
    mail = backend([{"done": 2, "errors": []}, {"done": 0, "errors": ["message 9 not found"]}], calls, limits=limits)
    mail.delete(("501", "502"))
    assert calls[0][0][4:] == ("Work", "INBOX", "archive", "Old", "[501, 502]")
    assert "Mail.move(" in calls[0][1] and "deletedStatus" in calls[0][1]
    with pytest.raises(MailError) as info:
        mail.delete(("9",))
    assert "not found" in str(info.value)
    mail.delete(())
    assert len(calls) == 2
    calls = []
    keep = backend([{"done": 1, "errors": []}], calls, limits=MailLimits(on_delete="keep"))
    keep.delete(("501",))
    assert calls[0][0][6] == "keep"


def test_send_parses_the_device_message():
    calls = []
    mail = backend([{"sent": True}, {"sent": False}], calls, sender="Me <me@example.org>")
    raw = b"From: me@example.org\r\nTo: bob@example.org\r\nSubject: From the Jornada\r\n\r\nHello from 1998\r\n"
    mail.send(raw, "me@example.org", ("bob@example.org", "carol@example.org"))
    assert calls[0][0][4:] == ("From the Jornada", "Hello from 1998\n", "Me <me@example.org>",
                               '["bob@example.org", "carol@example.org"]')
    assert "OutgoingMessage(" in calls[0][1] and "visible: false" in calls[0][1]
    with pytest.raises(MailError):
        mail.send(raw, "me@example.org", ("bob@example.org",))
    with pytest.raises(MailError):
        mail.send(raw, "me@example.org", ())
    assert len(calls) == 2


def test_build_from_account_and_helpers(tmp_path):
    ctx = BuildContext(log=lambda _l: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                       runner=fake_runner([{"mailboxes": 2}]))
    account = Account("a", "mail", "apple", (("mailbox", "Jornada"), ("local_user", "j"), ("on_delete", "delete")))
    mail = BACKEND.build(account, {"local_password": "x"}, ctx)
    mail.connect()
    assert BACKEND.missing_settings(Account("a", "mail", "apple"), {}) == ("local_user", "local_password")
    assert MODULE.backend("apple") is BACKEND
    assert parse_js_date("2026-09-06T10:00:00Z").hour == 10 and parse_js_date("nope") is None
    bare = build_message({"id": 1, "subject": "a\r\nb"})
    assert b"Subject: a b" in bare and b"From: unknown@jornada.invalid" in bare and b"To: undisclosed-recipients:;" in bare
    html_only = (b"Subject: s\r\nContent-Type: text/html\r\n\r\n<p>hi</p>\r\n")
    assert text_of(html_only) == ("s", "")
    assert text_of(b"Subject: t\r\n\r\nplain\r\ntext\r\n") == ("t", "plain\ntext\n")
