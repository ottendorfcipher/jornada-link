import io
import zipfile

import pytest

from jornada.sync.documents import xlsx
from jornada.sync.documents.csvtext import normalize_rows

ROWS = (("Name", "Qty", "Price", "Note"), ("Widget, large", "7", "1.5", 'say "hi" & <bye>'), ("ünï", "007", "1e+16"),
        (), ("multi\nline", "", "-2.5e-05"), ("x", "", "", "TRUE"))


def test_round_trip_keeps_text_numbers_and_shape():
    data = xlsx.from_rows(ROWS, "Budget 2026")
    assert xlsx.to_rows(data) == normalize_rows(ROWS)
    assert xlsx.from_rows(ROWS, "Budget 2026") == data  # deterministic bytes
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
    assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml",
            "xl/worksheets/sheet1.xml"} <= names
    assert '<c r="B2"><v>7</v></c>' in sheet and '<c r="C2"><v>1.5</v></c>' in sheet
    assert '<c r="B3" t="inlineStr"><is><t xml:space="preserve">007</t></is></c>' in sheet  # not numeric
    assert "&amp; &lt;bye&gt;" in sheet and '<row r="4">' not in sheet
    assert 'name="Budget 2026"' in workbook
    assert xlsx.to_rows(xlsx.from_rows(())) == ()


def test_sheet_names_are_sanitized():
    with zipfile.ZipFile(io.BytesIO(xlsx.from_rows((("a",),), 'Bad/Name:[x]*?"'))) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
    assert 'name="Bad_Name__x___&quot;"' in workbook
    with zipfile.ZipFile(io.BytesIO(xlsx.from_rows((("a",),), "   "))) as archive:
        assert 'name="Sheet1"' in archive.read("xl/workbook.xml").decode("utf-8")


def _package(parts):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _excel_like(sheet_xml, shared=None, target="/xl/worksheets/data.xml"):
    parts = {
        "xl/workbook.xml": f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets><sheet name="Data" sheetId="1" r:id="rId7"/>'
                           '<sheet name="Other" sheetId="2" r:id="rId8"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PKG}"><Relationship Id="rId8" Type="x" Target="worksheets/other.xml"/>'
                                      f'<Relationship Id="rId7" Type="x" Target="{target}"/></Relationships>',
        "xl/worksheets/data.xml": sheet_xml,
        "xl/worksheets/other.xml": f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>wrong</t></is></c></row></sheetData></worksheet>',
    }
    if shared is not None:
        parts["xl/sharedStrings.xml"] = shared
    return _package(parts)


def test_reads_shared_strings_rich_text_booleans_and_gaps():
    shared = (f'<sst xmlns="{NS}" count="3"><si><t>Plain</t></si><si><r><t>Ri</t></r><r><rPr/><t>ch</t></r>'
              '<rPh><t>skip</t></rPh></si><si><t xml:space="preserve"> spaced </t></si></sst>')
    sheet = (f'<worksheet xmlns="{NS}"><sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
             '<row r="3"><c r="A3" t="b"><v>1</v></c><c r="B3" t="b"><v>0</v></c><c r="C3"><v>2.50</v></c>'
             '<c r="D3" t="str"><f>A1&amp;"x"</f><v>Plainx</v></c><c r="E3" t="e"><v>#DIV/0!</v></c></row>'
             '<row><c><v>1E+16</v></c><c t="s"><v>2</v></c><c r="D4" s="1"/></row>'
             '</sheetData></worksheet>')
    assert xlsx.to_rows(_excel_like(sheet, shared)) == (
        ("Plain", "", "Rich"), (), ("TRUE", "FALSE", "2.5", "Plainx", "#DIV/0!"), ("1e+16", " spaced "))


def test_relative_targets_and_missing_shared_strings():
    sheet = f'<worksheet xmlns="{NS}"><sheetData><row r="2"><c r="B2" t="inlineStr"><is><r><t>a</t></r><r><t>b</t></r></is></c></row></sheetData></worksheet>'
    assert xlsx.to_rows(_excel_like(sheet, target="worksheets/data.xml")) == ((), ("", "ab"))
    assert xlsx.to_rows(_excel_like(f'<worksheet xmlns="{NS}"><sheetData/></worksheet>')) == ()


def test_malformed_packages_raise_value_error():
    with pytest.raises(ValueError):
        xlsx.to_rows(b"not a zip")
    with pytest.raises(ValueError):
        xlsx.to_rows(_package({"xl/workbook.xml": f'<workbook xmlns="{NS}"><sheets/></workbook>'}))
    with pytest.raises(ValueError):
        xlsx.to_rows(_package({"[Content_Types].xml": "<Types/>"}))
    with pytest.raises(ValueError):
        xlsx.to_rows(_excel_like(f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="A1" t="s"><v>5</v></c></row></sheetData></worksheet>',
                                 f'<sst xmlns="{NS}"/>'))
    with pytest.raises(ValueError):
        xlsx.to_rows(_excel_like("<worksheet><sheetData><row r='1'><c r='A1'><v>1</v></row></sheetData></worksheet>"))
    with pytest.raises(ValueError):
        xlsx.to_rows(_excel_like(f'<worksheet xmlns="{NS}"><sheetData><row r="9999999"><c r="A9999999"><v>1</v></c></row></sheetData></worksheet>'))
    with pytest.raises(ValueError):
        xlsx.to_rows(_excel_like(f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="1A"><v>1</v></c></row></sheetData></worksheet>'))


def test_column_helpers():
    assert [xlsx.column_letters(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]
    assert [xlsx.column_index(s) for s in ("A", "Z", "AA", "AB", "ZZ", "AAA")] == [0, 25, 26, 27, 701, 702]
    with pytest.raises(ValueError):
        xlsx.column_index("A1")
    with pytest.raises(ValueError):
        xlsx.column_letters(-1)
