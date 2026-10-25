import io
import zipfile

import pytest

from jornada.sync.documents import docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
ROUND_TRIPS = [
    "", "\n", "a", "a\n", "a\nb", "one\n\nthree\n", "tab\there\t", "<xml> & \"quotes\" 'apos'",
    "caf\u00e9 \u65e5\u672c \U0001F600", " leading and trailing ",
]


def package(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize("text", ROUND_TRIPS)
def test_round_trip(text):
    assert docx.to_text(docx.from_text(text)) == text


def test_writer_normalizes_and_strips_what_xml_cannot_carry():
    assert docx.to_text(docx.from_text("a\r\nb\rc")) == "a\nb\nc"
    assert docx.to_text(docx.from_text("ctrl\x01\x0cchar")) == "ctrlchar"
    assert docx.from_text("x") == docx.from_text("x")


def test_package_layout():
    with zipfile.ZipFile(io.BytesIO(docx.from_text("hi\tthere\n"))) as archive:
        names = archive.namelist()
        assert names[0] == "[Content_Types].xml" and "_rels/.rels" in names and "word/document.xml" in names
        types = archive.read("[Content_Types].xml").decode("utf-8")
        assert 'PartName="/word/document.xml"' in types and "wordprocessingml.document.main+xml" in types
        assert 'Target="word/document.xml"' in archive.read("_rels/.rels").decode("utf-8")
        document = archive.read("word/document.xml").decode("utf-8")
        assert document.count("<w:p>") + document.count("<w:p/>") == 2
        assert '<w:t xml:space="preserve">hi</w:t><w:tab/><w:t xml:space="preserve">there</w:t>' in document
        assert "<w:sectPr/>" in document and f'xmlns:w="{W}"' in document


def test_reader_walks_tables_breaks_and_drops_deleted_text():
    xml = f'''<w:document xmlns:w="{W}" xmlns:mc="{MC}"><w:body>
<w:p><w:r><w:t xml:space="preserve">Hello </w:t></w:r><w:r><w:t>world</w:t><w:br/><w:t>next</w:t></w:r>
<w:del><w:r><w:delText>gone</w:delText></w:r></w:del></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>cell2</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:r><mc:AlternateContent><mc:Choice><w:t>once</w:t></mc:Choice><mc:Fallback><w:t>once</w:t></mc:Fallback></mc:AlternateContent></w:r></w:p>
<w:p/>
<w:p><w:r><w:fldChar/><w:instrText>PAGE</w:instrText><w:t>1</w:t><w:tab/><w:t>x</w:t><w:noBreakHyphen/><w:softHyphen/><w:t>y</w:t></w:r></w:p>
<w:sectPr/></w:body></w:document>'''
    data = package({"word/document.xml": xml})
    assert docx.to_text(data) == "Hello world\nnext\ncell1\ncell2\nonce\n\n1\tx-y"


@pytest.mark.parametrize("data", [
    b"not a zip",
    package({"other.xml": "<a/>"}),
    package({"word/document.xml": "<broken"}),
    package({"word/document.xml": f'<w:document xmlns:w="{W}"/>'}),
])
def test_reader_rejects_what_is_not_a_document(data):
    with pytest.raises(ValueError):
        docx.to_text(data)
