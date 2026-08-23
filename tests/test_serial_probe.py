import io
import os
import pty
import threading

import pytest

from jornada import serial_probe


def test_modem_lines_from_bits():
    lines = serial_probe.ModemLines.from_bits(
        serial_probe.TIOCM_DTR | serial_probe.TIOCM_CTS | serial_probe.TIOCM_CAR
    )
    assert (lines.dtr, lines.rts, lines.cts, lines.dsr, lines.dcd) == (True, False, True, False, True)
    assert lines.describe() == "DTR=1 RTS=0 CTS=1 DSR=0 DCD=1"


def test_open_raw_rejects_unknown_baud(tmp_path):
    with pytest.raises(ValueError):
        serial_probe.open_raw(str(tmp_path / "nope"), 12345)


def test_sniff_sees_client_string_on_pty():
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    out = io.StringIO()

    def feed():
        threading.Event().wait(0.2)
        os.write(master, b"\x00CLIENT")

    threading.Thread(target=feed, daemon=True).start()
    captured = serial_probe.sniff(path, 19200, 1.0, out=out)
    os.close(master)
    os.close(slave)
    assert b"CLIENT" in captured
    text = out.getvalue()
    assert "modem lines: n/a" in text
    assert "saw 'CLIENT'" in text


def test_sniff_reports_silence(capsys):
    master, slave = pty.openpty()
    try:
        captured = serial_probe.sniff(os.ttyname(slave), 9600, 0.3)
    finally:
        os.close(master)
        os.close(slave)
    assert captured == b""
    assert "no bytes received" in capsys.readouterr().out


def test_main_usage_and_bad_device(capsys):
    assert serial_probe.main(["prog"]) == 2
    assert serial_probe.main(["prog", "/dev/definitely-not-here", "19200", "0.1"]) == 1
    assert "cannot use" in capsys.readouterr().err
