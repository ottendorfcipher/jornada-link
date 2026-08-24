"""Decode pppd `record` files (pppdump format) into readable PPP frames.

Record stream: type 1/2 = sent/rcvd data (u16 BE length + raw async-HDLC
bytes), 3/4 = end of direction, 5/6 = time delta in tenths (u32 BE / u8),
7 = absolute start time (u32 BE time_t). Frames are 0x7E-delimited with
0x7D escapes and a CRC-16/X.25 FCS.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

FLAG = 0x7E
ESCAPE = 0x7D
GOOD_FCS = 0xF0B8

PROTOCOLS = {
    0x0021: "IP",
    0x002D: "VJ-comp",
    0x002F: "VJ-uncomp",
    0x8021: "IPCP",
    0x80FD: "CCP",
    0xC021: "LCP",
    0xC023: "PAP",
    0xC223: "CHAP",
    0xC025: "LQR",
}

CP_CODES = {
    1: "Configure-Request", 2: "Configure-Ack", 3: "Configure-Nak",
    4: "Configure-Reject", 5: "Terminate-Request", 6: "Terminate-Ack",
    7: "Code-Reject", 8: "Protocol-Reject", 9: "Echo-Request",
    10: "Echo-Reply", 11: "Discard-Request",
}

LCP_OPTIONS = {
    1: "MRU", 2: "ACCM", 3: "Auth-Protocol", 4: "Quality-Protocol",
    5: "Magic-Number", 7: "PFC", 8: "ACFC", 13: "Callback", 17: "MRRU",
    18: "Short-Seq", 19: "Endpoint-Disc",
}

IPCP_OPTIONS = {
    1: "IP-Addresses", 2: "VJ-Compression", 3: "IP-Address",
    129: "Primary-DNS", 130: "Primary-NBNS", 131: "Secondary-DNS",
    132: "Secondary-NBNS",
}


def fcs16(data: bytes) -> int:
    fcs = 0xFFFF
    for byte in data:
        fcs ^= byte
        for _ in range(8):
            fcs = (fcs >> 1) ^ 0x8408 if fcs & 1 else fcs >> 1
    return fcs


@dataclass(frozen=True)
class Frame:
    direction: str          # "sent" | "rcvd"
    timestamp: float        # unix time (best effort)
    raw: bytes              # unescaped frame incl. FCS
    fcs_ok: bool
    protocol: Optional[int]
    payload: bytes


@dataclass
class _DirectionState:
    pending: bytearray = field(default_factory=bytearray)


def _unescape(chunk: bytes) -> bytes:
    out = bytearray()
    escaped = False
    for byte in chunk:
        if escaped:
            out.append(byte ^ 0x20)
            escaped = False
        elif byte == ESCAPE:
            escaped = True
        else:
            out.append(byte)
    return bytes(out)


def _parse_frame(direction: str, timestamp: float, body: bytes) -> Optional[Frame]:
    raw = _unescape(body)
    if len(raw) < 4:
        return None
    ok = fcs16(raw) == GOOD_FCS
    content = raw[:-2]
    if content[:2] == b"\xff\x03":
        content = content[2:]
    if not content:
        return None
    if content[0] & 1:
        protocol, payload = content[0], content[1:]
    elif len(content) >= 2:
        protocol, payload = (content[0] << 8) | content[1], content[2:]
    else:
        protocol, payload = None, b""
    return Frame(direction, timestamp, raw, ok, protocol, payload)


def parse_record_file(data: bytes) -> Iterator[Frame]:
    """Yield PPP frames from the raw bytes of a pppd record file."""
    pos = 0
    now = 0.0
    states = {"sent": _DirectionState(), "rcvd": _DirectionState()}

    def take(n: int) -> bytes:
        nonlocal pos
        chunk = data[pos:pos + n]
        pos += n
        return chunk

    frames: List[Tuple[str, float, bytes]] = []
    while pos < len(data):
        rtype = data[pos]
        pos += 1
        if rtype in (1, 2):
            direction = "sent" if rtype == 1 else "rcvd"
            length_bytes = take(2)
            if len(length_bytes) < 2:
                break
            length = (length_bytes[0] << 8) | length_bytes[1]
            state = states[direction]
            for byte in take(length):
                if byte == FLAG:
                    if state.pending:
                        frames.append((direction, now, bytes(state.pending)))
                        state.pending.clear()
                else:
                    state.pending.append(byte)
        elif rtype in (3, 4):
            direction = "sent" if rtype == 3 else "rcvd"
            state = states[direction]
            if state.pending:
                frames.append((direction, now, bytes(state.pending)))
                state.pending.clear()
        elif rtype == 5:
            now += int.from_bytes(take(4), "big") / 10.0
        elif rtype == 6:
            step = take(1)
            if step:
                now += step[0] / 10.0
        elif rtype == 7:
            now = float(int.from_bytes(take(4), "big"))
        else:
            break  # unknown record: stop rather than mis-parse
    for direction, timestamp, body in frames:
        frame = _parse_frame(direction, timestamp, body)
        if frame is not None:
            yield frame


def _ip(b: bytes) -> str:
    return ".".join(str(x) for x in b)


def describe_options(protocol: int, data: bytes) -> List[str]:
    names = LCP_OPTIONS if protocol == 0xC021 else IPCP_OPTIONS
    out = []
    pos = 0
    while pos + 2 <= len(data):
        opt_type, opt_len = data[pos], data[pos + 1]
        if opt_len < 2 or pos + opt_len > len(data):
            out.append(f"malformed-option type={opt_type}")
            break
        value = data[pos + 2:pos + opt_len]
        name = names.get(opt_type, f"opt{opt_type}")
        if protocol == 0x8021 and opt_type in (3, 129, 130, 131, 132) and len(value) == 4:
            out.append(f"{name}={_ip(value)}")
        elif opt_type == 3 and protocol == 0xC021 and len(value) >= 2:
            proto = (value[0] << 8) | value[1]
            out.append(f"{name}={PROTOCOLS.get(proto, hex(proto))}")
        elif value:
            out.append(f"{name}=0x{value.hex()}")
        else:
            out.append(name)
        pos += opt_len
    return out


def describe_frame(frame: Frame) -> str:
    when = _time.strftime("%H:%M:%S", _time.localtime(frame.timestamp)) if frame.timestamp > 1e9 \
        else f"+{frame.timestamp:7.1f}s"
    proto_name = PROTOCOLS.get(frame.protocol, hex(frame.protocol) if frame.protocol is not None else "?")
    prefix = f"{when} {frame.direction:4s} {proto_name:8s}"
    suffix = "" if frame.fcs_ok else "  [BAD FCS]"
    payload = frame.payload
    if frame.protocol in (0xC021, 0x8021, 0x80FD) and len(payload) >= 4:
        code, ident = payload[0], payload[1]
        length = (payload[2] << 8) | payload[3]
        body = payload[4:length] if length >= 4 else b""
        code_name = CP_CODES.get(code, f"code{code}")
        if code in (1, 2, 3, 4) and frame.protocol in (0xC021, 0x8021):
            detail = ", ".join(describe_options(frame.protocol, body)) or "(no options)"
        elif code == 8 and len(body) >= 2:
            rejected = (body[0] << 8) | body[1]
            detail = f"rejected-protocol={PROTOCOLS.get(rejected, hex(rejected))}"
        else:
            detail = body.hex(" ") if body else ""
        return f"{prefix} {code_name} id={ident} {detail}{suffix}"
    if frame.protocol == 0x0021 and len(payload) >= 20:
        header_len = (payload[0] & 0xF) * 4
        proto = payload[9]
        src, dst = _ip(payload[12:16]), _ip(payload[16:20])
        extra = ""
        if proto in (6, 17) and len(payload) >= header_len + 4:
            sport = (payload[header_len] << 8) | payload[header_len + 1]
            dport = (payload[header_len + 2] << 8) | payload[header_len + 3]
            extra = f" {sport}->{dport}"
        kind = {6: "TCP", 17: "UDP", 1: "ICMP"}.get(proto, f"ipproto{proto}")
        return f"{prefix} {kind} {src}->{dst}{extra} ({len(payload)}B){suffix}"
    return f"{prefix} {payload[:40].hex(' ')}{'...' if len(payload) > 40 else ''}{suffix}"


def dump(path: str) -> Iterator[str]:
    with open(path, "rb") as handle:
        data = handle.read()
    for frame in parse_record_file(data):
        yield describe_frame(frame)
