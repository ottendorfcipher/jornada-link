import struct

import pytest

from jornada import wire
from jornada.cedb import (
    CEDB_PROPDELETE, CEDB_PROPNOTFOUND, CEVT_BLOB, CEVT_I2, CEVT_LPWSTR, CedbError, PropVal, Record,
    align, filetime_to_unix, pack_record, propid, unix_to_filetime, unpack_record,
)


def test_align_rounds_up_to_four():
    assert [align(n) for n in (0, 1, 3, 4, 5, 8, 13)] == [0, 4, 4, 4, 8, 8, 16]


def test_propid_composition():
    assert propid(0x4223, CEVT_I2) == 0x42230002
    assert PropVal.string(0x37, "x").propid == 0x0037001F


def test_pack_layout_matches_librapi2():
    """Array of 16-byte CEPROPVALs, then 4-byte-aligned payloads addressed by offset."""
    props = (PropVal.i2(0x4223, 0), PropVal.string(0x0037, "Hi"), PropVal.blob(0x0017, b"abc"))
    packed = pack_record(props)
    expected = (
        wire.u32(0x42230002) + wire.u16(0) + wire.u16(0) + struct.pack("<i", 0) + wire.u32(0)
        + wire.u32(0x0037001F) + wire.u16(0) + wire.u16(0) + wire.u32(48) + wire.u32(0)
        + wire.u32(0x00170041) + wire.u16(0) + wire.u16(0) + wire.u32(3) + wire.u32(56)
        + "Hi".encode("utf-16-le") + b"\x00\x00" + b"\x00\x00"
        + b"abc" + b"\x00"
    )
    assert packed == expected
    assert len(packed) == 60


@pytest.mark.parametrize("prop", [
    PropVal.i2(1, -5), PropVal.i2(1, 0x7FFF), PropVal.ui2(2, 0xFFFF), PropVal.i4(3, -2_000_000_000),
    PropVal.ui4(4, 0xFFFFFFFF), PropVal.boolean(5, True), PropVal.boolean(5, False),
    PropVal.r8(6, 2.5), PropVal.filetime(7, 0x01D9E0F09B2C3D4E), PropVal.string(8, ""),
    PropVal.string(8, "héllo wörld — ünïcode"), PropVal.blob(9, b""), PropVal.blob(9, b"\x00\x01\x02"),
    PropVal.blob(9, bytes(range(256)) * 3 + b"x"),
])
def test_round_trip_every_kind(prop):
    unpacked = unpack_record(pack_record((prop,)), 1)
    assert unpacked == (prop,)


def test_round_trip_many_properties_keeps_order_and_alignment():
    props = tuple(PropVal.string(i, "s" * i) for i in range(1, 12)) + (PropVal.blob(99, b"z" * 7),)
    assert unpack_record(pack_record(props), len(props)) == props


def test_unpack_rejects_bad_offsets_and_counts():
    with pytest.raises(CedbError):
        unpack_record(b"\x00" * 15, 1)
    bad_string = wire.u32(0x0037001F) + wire.u32(0) + wire.u32(999) + wire.u32(0)
    with pytest.raises(CedbError):
        unpack_record(bad_string, 1)
    bad_blob = wire.u32(0x00170041) + wire.u32(0) + wire.u32(100) + wire.u32(16)
    with pytest.raises(CedbError):
        unpack_record(bad_blob, 1)


def test_unpack_marks_not_found_and_keeps_unknown_types_raw():
    entry = wire.u32(0x00010002) + wire.u16(0) + wire.u16(CEDB_PROPNOTFOUND) + bytes(8)
    (missing,) = unpack_record(entry, 1)
    assert missing.value is None and missing.flags & CEDB_PROPNOTFOUND
    unknown = wire.u32(0x0001007F) + wire.u32(0) + bytes(range(8))
    (raw,) = unpack_record(unknown, 1)
    assert raw.kind == 0x7F and raw.value == bytes(range(8)) and raw.kind_name == "0x007f"


def test_pack_range_checks():
    with pytest.raises(CedbError):
        pack_record((PropVal.ui2(1, -1),))
    with pytest.raises(CedbError):
        pack_record((PropVal(1, 0x7F, 1),))


def test_deleted_property_carries_flag_and_survives_packing():
    gone = PropVal.deleted(0x0017, CEVT_BLOB)
    assert gone.flags == CEDB_PROPDELETE and gone.value == b""
    (back,) = unpack_record(pack_record((gone,)), 1)
    assert back.flags == CEDB_PROPDELETE


def test_record_accessors_and_json():
    record = Record(0x30000001, (PropVal.string(0x37, "Lunch"), PropVal.blob(0x17, b"\x01\x02")))
    assert record.get(0x37).value == "Lunch" and record.get(0x99) is None
    assert record.value(0x99, "dflt") == "dflt"
    data = record.to_json()
    assert data["oid"] == 0x30000001
    assert data["props"][1] == {"id": 0x17, "kind": "blob", "value": "0102", "flags": 0}
    assert PropVal.i2(1, 2).kind_name == "i2" and PropVal.string(1, "x").kind == CEVT_LPWSTR


def test_filetime_helpers():
    assert filetime_to_unix(0) is None
    assert filetime_to_unix(unix_to_filetime(1_700_000_000.5)) == pytest.approx(1_700_000_000.5)
