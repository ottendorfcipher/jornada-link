import threading
from pathlib import Path

import pytest

from jornada import wire
from jornada.constants import DCCM_PING
from jornada.dccm import DccmError, DccmServer, Session, handle_header, handle_password_reply
from jornada.info import build_info_packet
from jornada.state import read_state
from tests.fake_device import JORNADA_INFO, FakeActiveSyncClient


def _collector():
    sent = []
    return sent, sent.append


def test_handle_header_ignores_empty_and_resets_ping_count():
    sent, send = _collector()
    session = Session(ip="1.2.3.4", missed_pings=2)
    assert handle_header(session, 0, lambda n: b"", send, None) == session
    pinged = handle_header(session, DCCM_PING, lambda n: b"", send, None)
    assert pinged.missed_pings == 0
    assert sent == []


def test_handle_header_info_packet_authenticates_and_pings():
    sent, send = _collector()
    body = build_info_packet(JORNADA_INFO)
    session = handle_header(Session(ip="x"), len(body), lambda n: body[:n], send, None)
    assert session.authenticated is True
    assert session.info == JORNADA_INFO
    assert sent == [wire.u32(DCCM_PING)]


def test_handle_header_rejects_tiny_info_packet():
    with pytest.raises(DccmError):
        handle_header(Session(ip="x"), 8, lambda n: b"\x00" * n, lambda d: None, None)


def test_password_challenge_flow():
    sent, send = _collector()
    session = handle_header(Session(ip="x"), 0x1000 | 0x21, lambda n: b"", send, "secret")
    assert session.locked and session.key == 0x21
    assert sent[0][:2] == wire.u16(len("secret") * 2 + 2)
    body = build_info_packet(JORNADA_INFO)
    session = handle_header(session, len(body), lambda n: body[:n], send, "secret")
    assert session.awaiting_password_reply and not session.authenticated
    session = handle_password_reply(session, b"\x01\x00", send)
    assert session.authenticated
    assert sent[-1] == wire.u32(DCCM_PING)
    with pytest.raises(DccmError):
        handle_password_reply(session, b"\x00\x00", send)


def test_password_challenge_without_password_is_fatal():
    with pytest.raises(DccmError):
        handle_header(Session(ip="x"), 0x1000, lambda n: b"", lambda d: None, None)


def test_server_end_to_end_with_fake_device(tmp_path: Path):
    state_path = tmp_path / "conn.json"
    connected = []
    server = DccmServer(bind_ip="127.0.0.1", port=0, state_path=state_path, allowed_peer="127.0.0.1",
                        ping_interval=0.05, on_connect=connected.append)
    server.open()
    result = {}

    def run():
        result["session"] = server.serve_one(timeout=5)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    device = FakeActiveSyncClient(server.port)
    device.connect()
    state = None
    for _ in range(50):
        state = read_state(state_path)
        if state:
            break
        threading.Event().wait(0.02)
    assert state and state["ip"] == "127.0.0.1"
    assert state["device"]["name"] == "Jornada680"
    device.answer_pings(3)
    device.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert connected and connected[0].authenticated
    assert device.pings_seen >= 4
    assert read_state(state_path) is None  # cleared on disconnect
    server.close()


def test_server_drops_silent_device(tmp_path: Path):
    server = DccmServer(bind_ip="127.0.0.1", port=0, state_path=tmp_path / "c.json", ping_interval=0.02, allowed_peer="127.0.0.1")
    server.open()
    result = {}
    thread = threading.Thread(target=lambda: result.update(session=server.serve_one(timeout=5)), daemon=True)
    thread.start()
    device = FakeActiveSyncClient(server.port)
    device.connect()  # answers nothing afterwards
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["session"].missed_pings >= 3
    device.close()
    server.close()


def test_server_with_password(tmp_path: Path):
    server = DccmServer(bind_ip="127.0.0.1", port=0, password="pw", state_path=tmp_path / "c.json", ping_interval=0.05, allowed_peer="127.0.0.1")
    server.open()
    thread = threading.Thread(target=lambda: server.serve_one(timeout=5), daemon=True)
    thread.start()
    device = FakeActiveSyncClient(server.port, password="pw", key=0x5A)
    device.connect()
    assert device.password_ok is True
    device.close()
    thread.join(timeout=5)
    server.close()


def test_server_rejects_unexpected_peer(tmp_path: Path):
    # allowed_peer is 10.0.0.1 but the fake device connects from 127.0.0.1
    server = DccmServer(bind_ip="127.0.0.1", port=0, state_path=tmp_path / "c.json",
                        ping_interval=0.05, allowed_peer="10.0.0.1")
    server.open()
    result = {}
    thread = threading.Thread(target=lambda: result.update(s=server.serve_one(timeout=2)), daemon=True)
    thread.start()
    device = FakeActiveSyncClient(server.port)
    try:
        device.connect()
    except (ConnectionError, OSError, AssertionError):
        pass  # server closes us before/at the handshake
    thread.join(timeout=3)
    assert result.get("s") is None  # connection was refused, no session
    device.close()
    server.close()
