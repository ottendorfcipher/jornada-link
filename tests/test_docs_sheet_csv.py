from datetime import datetime

import pytest

from jornada.pim.models import Document
from jornada.rapi import RapiClient
from jornada.sendmirror import ENV_MIRROR_DIR
from jornada.sync.accounts import Account, AccountError
from jornada.sync.documents import csvtext
from jornada.sync.documents.sheetcodec import (decode_sheet, device_folder_store, device_format_of, encode_sheet,
                                               sheet_codec)
from jornada.sync.registry import BuildContext
from tests.fake_device import FakeFilesystem, FakeRapiServer

ROWS = (("Name", "Qty", "Price", "Note"), ("Widget, large", "7", "1.5", 'say "hi"'), ("ünï", "007", "1e+16"),
        (), ("multi\nline",))


def test_csv_round_trip_keeps_quotes_commas_newlines_and_unicode():
    text = csvtext.rows_to_csv(ROWS)
    assert text == 'Name,Qty,Price,Note\n"Widget, large",7,1.5,"say ""hi"""\nünï,007,1e+16\n\n"multi\nline"\n'
    assert csvtext.csv_to_rows(text) == ROWS
    crlf = 'Name,Qty,Price,Note\r\n"Widget, large",7,1.5,"say ""hi"""\r\nünï,007,1e+16\r\n\r\n"multi\nline"\r\n'
    assert csvtext.csv_to_rows("\ufeff" + crlf) == ROWS
    assert csvtext.csv_to_rows("") == ()
    tsv = csvtext.rows_to_tsv(ROWS)
    assert "\t" in tsv and csvtext.tsv_to_rows(tsv) == ROWS
    with pytest.raises(ValueError):
        csvtext.csv_to_rows("x" * 200_000)  # over the csv module's field size limit


def test_normalize_pad_and_header_names():
    ragged = (("a", "", ""), ("", "", ""), ("b", "c", ""), (), ("", ""))
    assert csvtext.normalize_rows(ragged) == (("a",), (), ("b", "c"))
    assert csvtext.normalize_rows(()) == ()
    assert csvtext.pad_rows((("1",), ("1", "2", "3")), 2) == (("1", ""), ("1", "2"))
    assert csvtext.header_names(("", " a ", "A", "a", "b")) == ("c0001", "a", "A_2", "a_3", "b")
    assert csvtext.table_from_csv("x,y\n1\n2,3,4\n\n") == (("x", "y"), (("1", ""), ("2", "3")))
    with pytest.raises(ValueError):
        csvtext.table_from_csv("")
    with pytest.raises(ValueError):
        csvtext.table_from_csv("\n,\n")


def test_number_helpers_only_accept_canonical_text():
    assert csvtext.parse_number("7") == 7 and csvtext.parse_number("-3") == -3
    assert csvtext.parse_number("1.5") == 1.5 and csvtext.parse_number("1e+16") == 1e16
    assert csvtext.parse_number("2.5e-05") == 2.5e-05
    for text in ("007", "1.50", "-0", "1_0", "nan", "inf", ".5", "5.", "+3", " 3", "", "abc", "1,5"):
        assert csvtext.parse_number(text) is None, text
    assert csvtext.number_text(7) == "7" and csvtext.number_text(7.0) == "7" and csvtext.number_text(1.5) == "1.5"
    assert csvtext.number_text(True) == "TRUE" and csvtext.number_text(1e16) == "1e+16"
    assert csvtext.number_text(float("inf")) == "inf"


def test_iso_helpers():
    assert csvtext.iso_to_naive("2026-01-02") == datetime(2026, 1, 2)
    assert csvtext.iso_to_naive("2026-01-02T10:30") == datetime(2026, 1, 2, 10, 30)
    assert csvtext.iso_to_naive("2026-01-02 10:30:15") == datetime(2026, 1, 2, 10, 30, 15)
    assert csvtext.iso_to_naive("2026-01-02T10:30:15Z").tzinfo is None
    for text in ("2026-13-02", "hello", "", None, "2026-01-02T25:00", "20260102"):
        assert csvtext.iso_to_naive(text) is None
    assert csvtext.iso_cell_text(datetime(2026, 1, 2)) == "2026-01-02"
    assert csvtext.iso_cell_text(datetime(2026, 1, 2, 10, 30, 15, 999)) == "2026-01-02T10:30:15"


def test_decode_and_encode_sheet_in_both_device_formats():
    document = decode_sheet("Budget.csv", 'Item;Cost\r\nTea,"1,5"\r\n\r\n'.replace(";", ",").encode("cp1252"), 1_700_000_000.0)
    assert document == Document("Budget", 'Item,Cost\nTea,"1,5"\n', "sheet", datetime.fromtimestamp(1_700_000_000.0))
    assert encode_sheet(document) == ("Budget.csv", b'Item,Cost\r\nTea,"1,5"\r\n')
    assert encode_sheet(document, "tsv") == ("Budget.txt", b"Item\tCost\r\nTea\t1,5\r\n")
    assert decode_sheet("Budget.txt", b"Item\tCost\r\nTea\t1,5\r\n", None).text == document.text
    assert decode_sheet("noext", b"a,b", None).name == "noext"
    assert decode_sheet("Bad.csv", b"x", 1e300).modified is None
    assert sheet_codec().extensions == (".csv",) and sheet_codec("tsv").extensions == (".csv", ".txt")
    assert sheet_codec("tsv").encode(document)[0] == "Budget.txt"
    assert encode_sheet(Document("a/b:c", "x\n"))[0] == "a_b_c.csv"


@pytest.fixture
def device():
    fs = FakeFilesystem()
    fs.dirs.add("\\My Documents")
    fs.files["\\My Documents\\Prices.csv"] = b"Item,Cost\r\nTea,3\r\n"
    fs.files["\\My Documents\\Memo.txt"] = b"Item\tCost\r\nMilk\t2\r\n"
    fs.files["\\My Documents\\Report.pwd"] = b"\x00binary"
    server = FakeRapiServer(fs).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(device):
    with RapiClient("127.0.0.1", device.port, timeout=5) as c:
        yield c


def context(tmp_path):
    return BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _changes: None)


def test_device_folder_store_follows_account_settings(client, device, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MIRROR_DIR, str(tmp_path / "mirror"))
    csv_only = device_folder_store(client, Account("a", "documents", "excel"), context(tmp_path))
    assert csv_only.folder == "\\My Documents"
    assert [(i.id, i.record.name, i.record.text) for i in csv_only.list()] == [("Prices.csv", "Prices", "Item,Cost\nTea,3\n")]
    both = device_folder_store(client, Account("a", "documents", "sheets", (("device_format", "TSV"),)), context(tmp_path))
    assert sorted(i.id for i in both.list()) == ["Memo.txt", "Prices.csv"]
    name = both.create(Document("Fresh", "a,b\n1,2\n", "sheet"))
    assert name == "Fresh.txt" and device.fs.files["\\My Documents\\Fresh.txt"] == b"a\tb\r\n1\t2\r\n"
    elsewhere = device_folder_store(client, Account("a", "documents", "excel", (("folder", "\\Storage Card\\Sheets"),)),
                                    context(tmp_path))
    assert elsewhere.list() == () and "\\Storage Card\\Sheets" in device.fs.dirs
    with pytest.raises(AccountError):
        device_format_of(Account("a", "documents", "excel", (("device_format", "xls"),)))
