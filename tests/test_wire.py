import pytest

from jornada import wire


def test_u16_u32_little_endian():
    assert wire.u16(0x1234) == b"\x34\x12"
    assert wire.u32(0x12345678) == b"\x78\x56\x34\x12"


def test_string_matches_librapi2_layout():
    # 1, length-in-chars incl. NUL, UTF-16LE data
    assert wire.string("ab") == wire.u32(1) + wire.u32(3) + b"a\x00b\x00\x00\x00"
    assert wire.string(None) == wire.u32(0)


def test_optional_string_layout():
    assert wire.optional_string("a") == wire.u32(1) + wire.u32(4) + wire.u32(1) + b"a\x00\x00\x00"
    assert wire.optional_string(None) == wire.u32(0)


def test_optional_in_out():
    assert wire.optional_in(b"xyz") == wire.u32(1) + wire.u32(3) + b"xyz"
    assert wire.optional_in(None) == wire.u32(0)
    assert wire.optional_out(16) == wire.u32(1) + wire.u32(16) + wire.u32(0)
    assert wire.optional_out(None) == wire.u32(0)


def test_frame_prefixes_length():
    assert wire.frame(b"abc") == b"\x03\x00\x00\x00abc"


def test_reader_roundtrip_and_bounds():
    # Reply-side strings (rapi_buffer_read_string) are: length-without-NUL, then length+1 WCHARs
    data = wire.u32(7) + wire.u16(9) + wire.u32(2) + wire.wstr("hi")
    reader = wire.Reader(data)
    assert reader.u32() == 7
    assert reader.u16() == 9
    assert reader.string() == "hi"
    assert reader.remaining == 0
    with pytest.raises(wire.WireError):
        reader.u32()


def test_reader_optional_variants():
    assert wire.Reader(wire.u32(0)).optional() is None
    assert wire.Reader(wire.u32(1) + wire.u32(2) + wire.u32(0)).optional() is None
    assert wire.Reader(wire.u32(1) + wire.u32(2) + wire.u32(1) + b"ok").optional() == b"ok"


def test_reader_does_not_mutate_source():
    source = bytearray(b"\x01\x00\x00\x00")
    reader = wire.Reader(source)
    reader.u32()
    source[0] = 9
    assert wire.Reader(source).u32() == 9  # new reader sees change, old data copied
    assert reader.remaining == 0


def test_filetime_conversion():
    assert wire.filetime_to_unix(0, 0) is None
    # 2000-01-01T00:00:00Z = 125911584000000000 ticks
    assert wire.filetime_to_unix(125911584000000000 & 0xFFFFFFFF, 125911584000000000 >> 32) == 946684800.0
