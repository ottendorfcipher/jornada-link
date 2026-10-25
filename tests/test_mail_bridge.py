"""The POP3 server, the SMTP receiver, the shared backend and run_bridge, driven with poplib/smtplib."""
import poplib
import smtplib
import socket
import threading
import time

import pytest

from jornada.sync.accounts import Account
from jornada.sync.mail import MODULE, run_bridge
from jornada.sync.mail.backend import MailError
from jornada.sync.mail.bridge import MailBridge, SharedBackend, instructions
from jornada.sync.mail.config import BridgeConfig
from jornada.sync.mail.messages import (build_stub, dot_stuff, multiline_reply, normalize_crlf, pop3_uid,
                                        split_headers, top_lines)
from jornada.sync.mail.pop3 import Pop3Server
from jornada.sync.mail.smtp import SmtpServer
from jornada.sync.registry import BuildContext
from tests.test_mail_fakes import FakeMailBackend, message

LOCAL = "127.0.0.1"
CONFIG = BridgeConfig("jornada", "secret", listen=LOCAL, pop3_port=0, smtp_port=0, session_timeout=5.0)
MESSAGES = {
    "101": message("First", "hello\r\n.dot line\r\n..two dots\r\nbye"),
    "102": message("Second", "line 1\r\nline 2\r\nline 3"),
}
SIZE = {uid: len(raw) for uid, raw in MESSAGES.items()}


def serve(server_class, config, backend, log):
    server = server_class((LOCAL, 0), config, backend, log=log.append)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def log():
    return []


@pytest.fixture
def backend():
    return FakeMailBackend(MESSAGES)


@pytest.fixture
def pop3(backend, log):
    server = serve(Pop3Server, CONFIG, backend, log)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def smtp(backend, log):
    server = serve(SmtpServer, CONFIG, backend, log)
    yield server
    server.shutdown()
    server.server_close()


def pop_client(port):
    return poplib.POP3(LOCAL, port, timeout=5)


def login(port):
    client = pop_client(port)
    client.user("jornada")
    client.pass_("secret")
    return client


def smtp_client(port):
    client = smtplib.SMTP(LOCAL, port, timeout=5)
    client.ehlo()
    return client


# -- message helpers ---------------------------------------------------------------------
def test_message_helpers():
    assert normalize_crlf(b"a\nb\r\nc\rd") == b"a\r\nb\r\nc\r\nd"
    assert dot_stuff(b".a\r\nb\r\n..c") == b"..a\r\nb\r\n...c"
    assert multiline_reply(b"x") == b"x\r\n.\r\n" and multiline_reply(b"x\r\n") == b"x\r\n.\r\n"
    assert split_headers(b"A: 1\nB: 2\n\nbody\n") == (b"A: 1\r\nB: 2", b"body\r\n")
    assert split_headers(b"A: 1\r\n") == (b"A: 1", b"")
    assert top_lines(b"1\r\n2\r\n3", 2) == b"1\r\n2\r\n" and top_lines(b"1\r\n2", 0) == b""
    assert pop3_uid("abc-123") == "abc-123" and len(pop3_uid("with space")) == 40 and len(pop3_uid("x" * 80)) == 40
    stub = build_stub(b"Subject: Big\r\nFrom: a@b\r\nX-Spam: no\r\nContent-Type: text/html\r\n", 5_000_000, 262144)
    assert stub.startswith(b"Subject: Big\r\nFrom: a@b\r\nDate: ") and b"X-Spam" not in stub
    assert b"Content-Type: text/plain" in stub and b"4.8 MB" in stub and b"256 KB" in stub
    assert b"Subject: (message too large" in build_stub(b"", 10, 5)


# -- POP3 -----------------------------------------------------------------------------------
def test_pop3_login_ok_bad_and_lockout(pop3, backend, log):
    client = pop_client(pop3.port)
    assert client.getwelcome().startswith(b"+OK")
    with pytest.raises(poplib.error_proto):
        client.stat()  # not logged in
    with pytest.raises(poplib.error_proto):
        client.pass_("secret")  # USER first
    client.user("jornada")
    with pytest.raises(poplib.error_proto):
        client.pass_("wrong")
    client.user("nobody")
    with pytest.raises(poplib.error_proto):
        client.pass_("secret")
    client.user("jornada")
    with pytest.raises(poplib.error_proto):
        client.pass_("bad")  # third strike: the server hangs up
    with pytest.raises((poplib.error_proto, EOFError, OSError)):
        client.noop()
    good = login(pop3.port)
    assert good.stat() == (2, SIZE["101"] + SIZE["102"])
    with pytest.raises(poplib.error_proto):
        good.pass_("secret")  # already logged in
    good.quit()
    assert backend.connects == 0 and backend.deleted == []
    assert not any("secret" in line or "wrong" in line for line in log) and any("failed login (3/3)" in line for line in log)


def test_pop3_transaction_semantics(pop3, backend):
    client = login(pop3.port)
    _resp, listing, _octets = client.list()
    assert listing == [f"1 {SIZE['101']}".encode(), f"2 {SIZE['102']}".encode()]
    assert client.list(2) == f"+OK 2 {SIZE['102']}".encode()
    assert client.uidl()[1] == [b"1 101", b"2 102"] and client.uidl(1) == b"+OK 1 101"
    _resp, lines, octets = client.retr(1)
    assert b"\r\n".join(lines) == MESSAGES["101"] and octets == SIZE["101"] + 2
    assert backend.fetched == ["101"]
    client.retr(1)
    assert backend.fetched == ["101"]  # cached for the connection
    _resp, lines, _octets = client.top(2, 1)
    assert lines[0].startswith(b"From:") and lines[-2:] == [b"", b"line 1"]
    assert client._shortcmd("LAST") == b"+OK 2"
    client.dele(1)
    with pytest.raises(poplib.error_proto):
        client.retr(1)
    with pytest.raises(poplib.error_proto):
        client.dele(1)
    assert client.stat() == (1, SIZE["102"]) and client.list()[1] == [f"2 {SIZE['102']}".encode()]
    client.rset()
    assert client.stat() == (2, SIZE["101"] + SIZE["102"])
    client.dele(2)
    client.quit()
    assert backend.deleted == [("102",)] and "102" not in backend.messages


def test_pop3_top_zero_uses_headers_only(pop3, backend):
    client = login(pop3.port)
    _resp, lines, _octets = client.top(2, 0)
    assert lines[0].startswith(b"From:") and lines[-1] == b"" and b"line 1" not in lines
    assert backend.header_fetches == ["102"] and backend.fetched == []
    client.quit()


def test_pop3_capa_noop_and_errors(pop3, backend):
    client = login(pop3.port)
    assert {"TOP", "UIDL", "USER"} <= set(client.capa())
    client.noop()
    for bad in ("RETR 9", "RETR x", "TOP 1 x", "TOP 1", "DELE 0", "FROB", "LIST 3", "UIDL 0"):
        with pytest.raises(poplib.error_proto):
            client._shortcmd(bad)
    backend.fail_next = ["fetch"]
    with pytest.raises(poplib.error_proto):
        client.retr(2)
    assert client.retr(2)[2] == SIZE["102"] + 2
    client.quit()
    backend.fail_next = ["list"]
    client = pop_client(pop3.port)
    client.user("jornada")
    with pytest.raises(poplib.error_proto) as info:
        client.pass_("secret")
    assert "cannot open the mailbox" in str(info.value)
    client.user("jornada")
    client.pass_("secret")
    backend.fail_next = ["delete"]
    client.dele(1)
    with pytest.raises(poplib.error_proto) as info:
        client.quit()
    assert "not removed" in str(info.value)


def test_pop3_oversized_message_is_a_stub(log):
    big = message("Big", "x" * 400)
    backend = FakeMailBackend({"big": big, "small": MESSAGES["102"]})
    server = serve(Pop3Server, BridgeConfig("jornada", "secret", listen=LOCAL, pop3_port=0, max_size=300),
                   backend, log)
    try:
        client = login(server.port)
        count, total = client.stat()
        sizes = [int(line.split()[1]) for line in client.list()[1]]
        assert count == 2 and sizes[0] < len(big) and total == sum(sizes)
        _resp, lines, octets = client.retr(1)
        assert octets == sizes[0]
        assert b"Subject: Big" in lines and b"X-Jornada-Bridge: stub; original-size=%d" % len(big) in lines
        assert any(b"above the max_size" in line for line in lines) and not any(b"xxxx" in line for line in lines)
        assert lines[0].startswith(b"From: alice@example.org")
        assert client.top(1, 0)[1][-1] == b""
        assert backend.header_fetches == ["big"] and backend.fetched == []
        assert client.retr(2)[2] == SIZE["102"] + 2
        client.quit()
    finally:
        server.shutdown()
        server.server_close()


def test_pop3_dot_stuffing_on_the_wire(pop3):
    with socket.create_connection((LOCAL, pop3.port), timeout=5) as sock:
        sock.sendall(b"USER jornada\r\nPASS secret\r\nRETR 1\r\nQUIT\r\n")
        data = b""
        while b"+OK bye" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    assert b"\r\n..dot line\r\n...two dots\r\nbye\r\n.\r\n+OK bye" in data
    assert b"+OK %d octets" % (SIZE["101"] + 2) in data


# -- SMTP --------------------------------------------------------------------------------------
def test_smtp_auth_plain_and_login(smtp, log):
    client = smtp_client(smtp.port)
    assert client.has_extn("auth") and "PLAIN" in client.esmtp_features["auth"] and client.has_extn("size")
    assert client.login("jornada", "secret")[0] == 235  # PLAIN with an initial response
    assert client.auth("PLAIN", client.auth_plain)[0] == 503  # already authenticated
    client.quit()
    client = smtp_client(smtp.port)
    client.user, client.password = "jornada", "secret"
    assert client.auth("LOGIN", client.auth_login)[0] == 235
    client.quit()
    client = smtp_client(smtp.port)
    client.user, client.password = "jornada", "secret"
    assert client.auth("PLAIN", client.auth_plain, initial_response_ok=False)[0] == 235
    client.quit()
    client = smtp_client(smtp.port)
    client.user, client.password = "jornada", "secret"
    assert client.auth("LOGIN", client.auth_login, initial_response_ok=False)[0] == 235
    client.quit()
    assert not any("secret" in line for line in log) and any("authenticated as 'jornada'" in line for line in log)


def test_smtp_auth_failures_and_lockout(smtp):
    client = smtp_client(smtp.port)
    client.user, client.password = "jornada", "nope"
    with pytest.raises(smtplib.SMTPAuthenticationError):
        client.auth("PLAIN", client.auth_plain)
    client.user, client.password = "nobody", "secret"
    with pytest.raises(smtplib.SMTPAuthenticationError):
        client.auth("LOGIN", client.auth_login)
    assert client.docmd("AUTH", "LOGIN")[0] == 334 and client.docmd("*")[0] == 501  # aborted
    assert client.docmd("AUTH", "CRAM-MD5")[0] == 504
    assert client.docmd("AUTH", "PLAIN not*base64")[0] == 501
    assert client.docmd("AUTH", "PLAIN " + smtplib.base64.b64encode(b"two\0parts").decode())[0] == 501
    with pytest.raises(smtplib.SMTPAuthenticationError) as info:
        client.auth("PLAIN", client.auth_plain)  # third failure: 421 and the server hangs up
    assert info.value.smtp_code == 421
    with pytest.raises((smtplib.SMTPServerDisconnected, OSError)):
        client.noop()


def test_smtp_delivers_data_to_the_backend(smtp, backend):
    client = smtp_client(smtp.port)
    client.login("jornada", "secret")
    raw = b"Subject: hi\r\nTo: bob@example.org\r\n\r\nline\r\n.dot\r\n..dots\r\n"
    assert client.sendmail("me@example.org", ["bob@example.org", "carol@example.org"], raw) == {}
    assert backend.sent == [(raw, "me@example.org", ("bob@example.org", "carol@example.org"))]
    assert client.sendmail("", ["bob@example.org"], b"Subject: bounce\n\nno CR here") == {}
    assert backend.sent[1][0] == b"Subject: bounce\r\n\r\nno CR here\r\n" and backend.sent[1][1] == ""
    client.quit()


def test_smtp_send_failure_answers_451(smtp, backend):
    backend.fail_send = "IMAP server is unreachable"
    client = smtp_client(smtp.port)
    client.login("jornada", "secret")
    with pytest.raises(smtplib.SMTPDataError) as info:
        client.sendmail("me@example.org", ["bob@example.org"], b"Subject: x\r\n\r\nbody\r\n")
    assert info.value.smtp_code == 451 and b"unreachable" in info.value.smtp_error
    assert client.noop()[0] == 250  # the session survives
    client.quit()


def test_smtp_envelope_rules(smtp):
    client = smtp_client(smtp.port)
    with pytest.raises(smtplib.SMTPSenderRefused) as info:
        client.sendmail("me@example.org", ["bob@example.org"], b"x")
    assert info.value.smtp_code == 530
    client.login("jornada", "secret")
    assert client.docmd("RCPT", "TO:<a@b>")[0] == 503 and client.docmd("DATA")[0] == 503
    assert client.docmd("MAIL", "FROM:<bad address>")[0] == 501 and client.docmd("MAIL", "nonsense")[0] == 501
    assert client.docmd("MAIL", "FROM:<me@x> SIZE=99999999999")[0] == 552
    assert client.docmd("MAIL", "FROM:<>")[0] == 250 and client.docmd("MAIL", "FROM:<c@d>")[0] == 503
    assert client.docmd("RCPT", "TO:<nobody>")[0] == 501 and client.docmd("RCPT", "TO:<a@b>")[0] == 250
    assert client.docmd("RSET")[0] == 250 and client.docmd("DATA")[0] == 503
    assert client.docmd("FROB")[0] == 500 and client.docmd("VRFY", "x")[0] == 252 and client.docmd("STARTTLS")[0] == 502
    assert client.docmd("HELO")[0] == 501 and client.docmd("HELO", "jornada")[0] == 250 and client.docmd("HELP")[0] == 214
    client.quit()


def test_smtp_size_cap_and_auth_off(backend, log):
    config = BridgeConfig("jornada", "secret", listen=LOCAL, smtp_port=0, smtp_auth=False, smtp_max_message=100)
    server = serve(SmtpServer, config, backend, log)
    try:
        client = smtp_client(server.port)
        # smtplib refuses locally when the message exceeds the advertised SIZE; a client
        # that ignores the extension gets the same 552 from the server after DATA.
        with pytest.raises((smtplib.SMTPDataError, smtplib.SMTPSenderRefused)) as info:
            client.sendmail("me@example.org", ["bob@example.org"], b"Subject: big\r\n\r\n" + b"y" * 300)
        assert info.value.smtp_code == 552
        assert client.docmd("MAIL", "FROM:<me@example.org>")[0] == 250
        assert client.docmd("RCPT", "TO:<bob@example.org>")[0] == 250
        assert client.docmd("DATA")[0] == 354
        client.send(b"y" * 300 + b"\r\n.\r\n")
        assert client.getreply()[0] == 552
        assert client.sendmail("me@example.org", ["bob@example.org"], b"Subject: ok\r\n\r\nsmall\r\n") == {}
        assert backend.sent[0][1] == "me@example.org"
        assert client.login("jornada", "secret")[0] == 235  # AUTH still works when optional
        client.quit()
    finally:
        server.shutdown()
        server.server_close()


# -- the shared backend and the bridge ----------------------------------------------------------
def test_shared_backend_connects_lazily_and_reconnects_once(log):
    backend = FakeMailBackend(MESSAGES)
    shared = SharedBackend(backend, log.append)
    assert backend.connects == 0 and shared.name == "fake"
    assert len(shared.list()) == 2 and backend.connects == 1
    backend.fail_next = ["fetch"]
    assert shared.fetch("102") == MESSAGES["102"] and (backend.connects, backend.closes) == (2, 1)
    backend.fail_next = ["fetch", "fetch"]
    with pytest.raises(MailError):
        shared.fetch("101")
    assert (backend.connects, backend.closes) == (3, 3)
    shared.send(b"x", "a@b", ["c@d"])
    assert backend.sent[0][2] == ("c@d",) and backend.connects == 4
    shared.close()
    assert backend.closes == 4 and any("reconnecting" in line for line in log)
    failing = FakeMailBackend({}, fail_connect="no network")
    with pytest.raises(MailError):
        SharedBackend(failing).list()
    assert failing.connects == 1


def test_bridge_serves_both_protocols_and_stops(backend):
    bridge = MailBridge(backend, CONFIG)
    bridge.start()
    with pytest.raises(RuntimeError):
        bridge.start()
    client = login(bridge.pop3_port)
    assert client.stat()[0] == 2 and backend.connects == 1
    client.quit()
    mailer = smtp_client(bridge.smtp_port)
    mailer.login("jornada", "secret")
    mailer.sendmail("me@example.org", ["bob@example.org"], b"Subject: via bridge\r\n\r\nhi\r\n")
    mailer.quit()
    assert backend.sent[0][1] == "me@example.org"
    port = bridge.pop3_port
    bridge.stop()
    assert backend.closes == 1
    with pytest.raises(OSError):
        poplib.POP3(LOCAL, port, timeout=1)


def context(tmp_path, log, **extra):
    return BuildContext(log=log.append, sync_dir=tmp_path, save_secrets=lambda changes: None, extra=extra)


def account(**settings):
    base = {"local_user": "jornada", "listen": LOCAL, "pop3_port": "0", "smtp_port": "0", **settings}
    return Account("m", "mail", "imap", tuple(sorted(base.items())))


def test_module_spec_and_bridge_refuses_without_local_credentials(tmp_path):
    assert MODULE.key == "mail" and MODULE.is_bridge and MODULE.device_store is None and MODULE.bridge is run_bridge
    assert [s.key for s in MODULE.settings] == ["listen", "pop3_port", "smtp_port", "smtp_auth", "allow_any_interface"]
    assert all(not s.required for s in MODULE.settings)
    assert [b.key for b in MODULE.backends] == ["imap", "gmail", "m365", "apple"]
    for backend in MODULE.backends:
        secret = {s.key: s.secret for s in backend.settings}
        assert secret["local_user"] is False and secret["local_password"] is True
        assert {"max_messages", "max_size", "on_delete", "archive_mailbox"} <= set(secret)
    assert MODULE.backend("gmail").login is not None and MODULE.backend("imap").login is None
    log = []
    assert MODULE.bridge(Account("m", "mail", "imap", (("listen", LOCAL),)), {}, context(tmp_path, log)) == 1
    assert log and "local_user" in log[0] and "local_password" in log[0]
    log.clear()
    assert run_bridge(account(pop3_port="abc"), {"local_password": "secret"}, context(tmp_path, log)) == 1
    assert "pop3_port" in log[0]


def test_run_bridge_reports_connect_and_bind_failures(tmp_path):
    log = []
    failing = FakeMailBackend({}, fail_connect="IMAP unreachable")
    assert run_bridge(account(), {"local_password": "secret"}, context(tmp_path, log, mail_backend=failing)) == 1
    assert any("IMAP unreachable" in line for line in log)
    log.clear()
    backend = FakeMailBackend(MESSAGES)
    ctx = context(tmp_path, log, mail_backend=backend)
    assert run_bridge(account(listen="203.0.113.1"), {"local_password": "secret"}, ctx) == 1
    assert any("cannot listen on 203.0.113.1" in line for line in log) and backend.closes == 1
    lines = instructions(account(email="me@example.org"), BridgeConfig("jornada", "secret", listen=LOCAL), 1110, 1025)
    assert any("User ID: jornada" in line for line in lines) and any("1110/1025" in line for line in lines)
    assert any("return address: me@example.org" in line for line in lines)
    assert not any("secret" in line for line in lines)


def test_run_bridge_serves_until_stopped(tmp_path):
    log, started, results = [], [], []
    backend = FakeMailBackend(MESSAGES)
    ctx = context(tmp_path, log, mail_backend=backend, mail_on_start=started.append)
    worker = threading.Thread(target=lambda: results.append(run_bridge(account(), {"local_password": "secret"}, ctx)),
                              daemon=True)
    worker.start()
    deadline = time.time() + 5
    while not started and time.time() < deadline:
        time.sleep(0.02)
    assert started, log
    bridge = started[0]
    client = login(bridge.pop3_port)
    assert client.stat()[0] == 2
    client.quit()
    bridge.stop()
    worker.join(5)
    assert results == [0] and backend.connects == 1
    assert any("POP3 port" in line for line in log) and any("User ID: jornada" in line for line in log)
    assert not any("secret" in line for line in log)
