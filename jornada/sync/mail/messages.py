"""Byte-level helpers for RFC 2822 messages on the POP3/SMTP wire: line endings,
dot-stuffing, header selection, and the stub served instead of an oversized message."""
from __future__ import annotations

import hashlib
from email.utils import formatdate
from typing import Iterable, Tuple

CRLF = b"\r\n"
UIDL_MAX_LENGTH = 70
STUB_HEADERS = (b"date", b"from", b"to", b"cc", b"reply-to", b"subject", b"message-id", b"in-reply-to",
                b"references")
STUB_MARKER = b"X-Jornada-Bridge"


def normalize_crlf(raw: bytes) -> bytes:
    """Every line ends with CRLF, whatever the source used."""
    return raw.replace(CRLF, b"\n").replace(b"\r", b"\n").replace(b"\n", CRLF)


def ensure_final_crlf(raw: bytes) -> bytes:
    return raw if not raw or raw.endswith(CRLF) else raw + CRLF


def dot_stuff(raw: bytes) -> bytes:
    """Prefix lines that start with a dot (RFC 1939 §3 / RFC 5321 §4.5.2); ``raw`` must be CRLF-normalised."""
    return CRLF.join(b"." + line if line.startswith(b".") else line for line in raw.split(CRLF))


def dot_unstuff(line: bytes) -> bytes:
    return line[1:] if line.startswith(b"..") else line


def multiline_reply(payload: bytes) -> bytes:
    """A stuffed multi-line body with its terminating ``.`` line."""
    return dot_stuff(ensure_final_crlf(payload)) + b"." + CRLF


def split_headers(raw: bytes) -> Tuple[bytes, bytes]:
    """(header block without the blank line, body), both CRLF-normalised."""
    normalized = normalize_crlf(raw)
    index = normalized.find(CRLF + CRLF)
    if index < 0:
        return normalized.rstrip(b"\r\n"), b""
    return normalized[:index], normalized[index + 4:]


def top_lines(body: bytes, count: int) -> bytes:
    """The first ``count`` lines of a CRLF-normalised body, each CRLF-terminated."""
    if count <= 0 or not body:
        return b""
    lines = ensure_final_crlf(body).split(CRLF)[:-1]
    return b"".join(line + CRLF for line in lines[:count])


def header_entries(header_block: bytes) -> Tuple[bytes, ...]:
    """Header fields of a CRLF-normalised block, continuation lines kept with their field."""
    entries: Tuple[bytes, ...] = ()
    for line in header_block.split(CRLF):
        if not line:
            continue
        if line[:1] in (b" ", b"\t") and entries:
            entries = entries[:-1] + (entries[-1] + CRLF + line,)
        else:
            entries += (line,)
    return entries


def select_headers(header_block: bytes, names: Iterable[bytes] = STUB_HEADERS) -> Tuple[bytes, ...]:
    wanted = tuple(name.lower() for name in names)
    return tuple(entry for entry in header_entries(header_block)
                 if entry.split(b":", 1)[0].strip().lower() in wanted)


def pop3_uid(uid: str) -> str:
    """A UIDL-safe id: 1-70 printable ASCII characters, derived deterministically otherwise."""
    if 0 < len(uid) <= UIDL_MAX_LENGTH and all(0x21 <= ord(ch) <= 0x7E for ch in uid):
        return uid
    return hashlib.sha1(uid.encode("utf-8", "surrogateescape")).hexdigest()


def human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} bytes"


def build_stub(header_block: bytes, size: int, limit: int) -> bytes:
    """A small message carrying the original's key headers and a note about its size."""
    kept = select_headers(normalize_crlf(header_block))
    present = {entry.split(b":", 1)[0].strip().lower() for entry in kept}
    if b"subject" not in present:
        kept += (b"Subject: (message too large for the device)",)
    if b"from" not in present:
        kept += (b"From: Jornada mail bridge <bridge@jornada.invalid>",)
    if b"date" not in present:
        kept += (b"Date: " + formatdate(localtime=True).encode("ascii"),)
    body = (
        f"[Jornada mail bridge] This message is {human_size(size)}, above the max_size of "
        f"{human_size(limit)} set for the device, so only its headers were sent. Read it on the Mac, or "
        "raise max_size on the sync account (--set max_size=BYTES) and download again."
    ).encode("ascii")
    fixed = (STUB_MARKER + b": stub; original-size=" + str(size).encode("ascii"),
             b"MIME-Version: 1.0", b"Content-Type: text/plain; charset=us-ascii",
             b"Content-Transfer-Encoding: 7bit")
    return CRLF.join(kept + fixed) + CRLF + CRLF + body + CRLF
