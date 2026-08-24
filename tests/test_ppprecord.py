import time

from jornada import ppprecord
from jornada.ppprecord import Frame, describe_frame, fcs16, parse_record_file

# Real LCP Configure-Request captured from the Jornada 680e (2026-08-23):
JORNADA_LCP = bytes.fromhex(
    "7e ff 7d 23 c0 21 7d 21 7d 21 7d 20 7d 2e 7d 22 7d 26"
    "7d 20 7d 20 7d 20 7d 20 7d 27 7d 22 7d 28 7d 22 70 34 7e".replace(" ", "")
)


def _record(rtype: int, payload: bytes) -> bytes:
    """Data record (types 1/2): u16 BE length + bytes."""
    return bytes([rtype, len(payload) >> 8, len(payload) & 0xFF]) + payload


def _start_time(when: int) -> bytes:
    """Type-7 record: absolute start time, no length prefix."""
    return bytes([7]) + when.to_bytes(4, "big")


def test_fcs16_matches_known_good_frame():
    unescaped = bytes.fromhex("ff03c021" + "0101000e020600000000070208 02".replace(" ", "") + "7034")
    assert fcs16(unescaped) == ppprecord.GOOD_FCS


def test_parse_real_jornada_lcp_frame():
    data = _start_time(1700000000) + _record(2, JORNADA_LCP)
    frames = list(parse_record_file(data))
    assert len(frames) == 1
    frame = frames[0]
    assert frame.direction == "rcvd"
    assert frame.fcs_ok is True
    assert frame.protocol == 0xC021
    text = describe_frame(frame)
    assert "Configure-Request id=1" in text
    assert "ACCM=0x00000000" in text
    assert "PFC" in text and "ACFC" in text
    assert "BAD FCS" not in text


def test_frame_split_across_records_and_time():
    half = len(JORNADA_LCP) // 2
    data = (
        _start_time(1700000000)
        + _record(2, JORNADA_LCP[:half])
        + bytes([6, 5])  # +0.5s
        + _record(2, JORNADA_LCP[half:])
        + _record(1, JORNADA_LCP)  # same bytes "sent" back
        + bytes([3])  # end send
    )
    frames = list(parse_record_file(data))
    assert [f.direction for f in frames] == ["rcvd", "sent"]
    assert frames[0].raw == frames[1].raw
    # the frame completes after the mid-frame +0.5s time step
    assert frames[0].timestamp == 1700000000.5


def test_bad_fcs_flagged():
    corrupted = bytearray(JORNADA_LCP)
    corrupted[10] ^= 0xFF
    frames = list(parse_record_file(_record(2, bytes(corrupted))))
    assert frames and frames[0].fcs_ok is False
    assert "BAD FCS" in describe_frame(frames[0])


def test_ipcp_and_ip_and_protocol_reject_rendering():
    # IPCP Configure-Request with IP-Address 192.168.131.201
    ipcp = bytes.fromhex("8021") + bytes([1, 5, 0, 10]) + bytes([3, 6, 192, 168, 131, 201])
    fcs = fcs16(ipcp) ^ 0xFFFF
    frame = ppprecord._parse_frame("rcvd", 0.0, ipcp + bytes([fcs & 0xFF, fcs >> 8]))
    assert frame is not None and frame.fcs_ok
    assert "IP-Address=192.168.131.201" in describe_frame(frame)

    # Minimal TCP/IP packet 192.168.131.201 -> 192.168.131.102 port 1024->990
    ip_header = bytes.fromhex("45000028000040004006") + b"\x00\x00" + bytes([192, 168, 131, 201, 192, 168, 131, 102])
    tcp = (1024).to_bytes(2, "big") + (990).to_bytes(2, "big") + b"\x00" * 16
    payload = bytes.fromhex("0021") + ip_header + tcp
    fcs = fcs16(payload) ^ 0xFFFF
    frame = ppprecord._parse_frame("rcvd", 0.0, payload + bytes([fcs & 0xFF, fcs >> 8]))
    text = describe_frame(frame)
    assert "TCP 192.168.131.201->192.168.131.102 1024->990" in text

    # LCP Protocol-Reject of CCP
    lcp = bytes.fromhex("c021") + bytes([8, 2, 0, 6]) + bytes.fromhex("80fd")
    fcs = fcs16(lcp) ^ 0xFFFF
    frame = ppprecord._parse_frame("sent", 0.0, lcp + bytes([fcs & 0xFF, fcs >> 8]))
    assert "rejected-protocol=CCP" in describe_frame(frame)


def test_unknown_record_type_stops_cleanly():
    data = _record(2, JORNADA_LCP) + b"\xff garbage"
    frames = list(parse_record_file(data))
    assert len(frames) == 1
