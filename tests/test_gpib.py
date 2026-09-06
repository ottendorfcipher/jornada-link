from jornada import cli
from jornada.gpib import GatewayAddress, GpibError, GpibGateway, escape
from tests.fake_gateway import FakeGateway

import time

import pytest


def wait_for(gateway, line, timeout=2.0):
    """Writes are fire-and-forget on the client side; give the fake gateway time to log them."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if line in gateway.received:
            return
        time.sleep(0.01)
    raise AssertionError(f"{line!r} never reached the gateway: {gateway.received}")


@pytest.fixture
def gateway():
    g = FakeGateway().start()
    try:
        yield g
    finally:
        g.stop()


def test_escape_marks_protocol_bytes():
    assert escape(b"a+b\r\n\x1b") == b"a\x1b+b\x1b\r\x1b\n\x1b\x1b"
    assert escape(b"plain") == b"plain"


def test_query_and_serial_poll(gateway):
    with GpibGateway(GatewayAddress("127.0.0.1", gateway.port), timeout=2) as gw:
        assert gw.version() == "fake gateway 1.0"
        assert gw.query(1, "*IDN?") == "FAKE,TDS 340,0,FV:v1.02"
        assert gw.serial_poll(1) == 64
        gw.write(1, "*RST")
        wait_for(gateway, "*RST")
        assert gateway.received[-1] == "*RST"
        gw.interface_clear()
        gw.device_clear(1)
        gw.trigger(1)
        gw.local(1)
        wait_for(gateway, "++loc")
        assert gateway.received[-7:] == ["++ifc", "++addr 1", "++clr", "++addr 1", "++trg", "++addr 1", "++loc"]
        with pytest.raises(GpibError):
            gw.set_address(31)
        with pytest.raises(GpibError):
            gw.read(2, timeout=0.3)   # nothing pending at address 2


def test_connection_refused_is_a_gpib_error():
    with pytest.raises(GpibError):
        GpibGateway(GatewayAddress("127.0.0.1", 1), timeout=0.5).connect()


def test_cli_gpib_actions(gateway, capsys):
    base = ["--ip", "127.0.0.1", "gpib", "--gateway-port", str(gateway.port)]
    assert cli.main(base + ["idn"]) == 0
    assert "FAKE,TDS 340" in capsys.readouterr().out
    assert cli.main(base + ["query", "*ESR?"]) == 0
    assert capsys.readouterr().out.strip() == "128"
    assert cli.main(base + ["spoll"]) == 0
    assert "0x40" in capsys.readouterr().out
    assert cli.main(base + ["write", "ACQUIRE:STATE", "RUN"]) == 0
    wait_for(gateway, "ACQUIRE:STATE RUN")
    assert gateway.received[-1] == "ACQUIRE:STATE RUN"
    assert cli.main(base + ["ver"]) == 0
    assert cli.main(base + ["ifc"]) == 0
    assert cli.main(base + ["-a", "2", "read"]) == 1     # timeout at an idle address
    assert "no answer" in capsys.readouterr().err


def test_cli_gpib_unreachable(capsys):
    assert cli.main(["--ip", "127.0.0.1", "gpib", "--gateway-port", "1", "--timeout", "0.5", "idn"]) == 1
    assert "cannot reach the GPIB gateway" in capsys.readouterr().err


def test_repl_reply_detection():
    assert cli._gpib_replies("++ver")
    assert cli._gpib_replies("++addr")
    assert not cli._gpib_replies("++addr 1")
    assert cli._gpib_replies("++read eoi")
    assert not cli._gpib_replies("++ifc")
    assert not cli._gpib_replies("++")
