"""Word .docx as text: paragraphs in, paragraphs out (``zipfile`` + ElementTree, nothing else).

``to_text`` reads the paragraphs of ``word/document.xml`` (table cells and text
boxes included, tracked deletions and field codes excluded). ``from_text``
writes the smallest valid package Word opens: content types, the package
relationships and one ``w:p`` per line.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import List, Tuple
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
DOCUMENT_PART = "word/document.xml"
MAIN_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"

_W = "{" + W_NS + "}"
_ALTERNATE = "{" + MC_NS + "}AlternateContent"
_DROPPED = frozenset({_W + "delText", _W + "instrText", _W + "delInstrText"})
_MARKS = {_W + "tab": "\t", _W + "ptab": "\t", _W + "br": "\n", _W + "cr": "\n",
          _W + "noBreakHyphen": "-", _W + "softHyphen": ""}
_XML_UNSAFE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FIXED_STAMP = (1980, 1, 1, 0, 0, 0)
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

CONTENT_TYPES = (
    _XML_HEAD
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/{DOCUMENT_PART}" ContentType="{MAIN_CONTENT_TYPE}"/>'
    "</Types>"
)
PACKAGE_RELS = (
    _XML_HEAD
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    f'Target="{DOCUMENT_PART}"/>'
    "</Relationships>"
)
DOCUMENT_RELS = (
    _XML_HEAD
    + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
)


# -- reading --------------------------------------------------------------------
def to_text(data: bytes) -> str:
    """The paragraphs of a .docx joined by ``\\n``; raises ValueError for anything that is not one."""
    root = _document_root(data)
    body = root.find(_W + "body")
    if body is None:
        raise ValueError(f"{DOCUMENT_PART} has no w:body")
    return "\n".join(_paragraphs(body))


def _document_root(data: bytes) -> ET.Element:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read(DOCUMENT_PART)
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError) as exc:
        raise ValueError(f"not a .docx document: {exc}") from exc
    try:
        return ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"{DOCUMENT_PART} is not well-formed XML: {exc}") from exc


def _paragraphs(container: ET.Element) -> List[str]:
    """Paragraph texts in document order, descending into tables and content controls."""
    found: List[str] = []
    for child in container:
        if child.tag == _W + "p":
            found.append(_paragraph_text(child))
        else:
            found.extend(_paragraphs(child))
    return found


def _paragraph_text(paragraph: ET.Element) -> str:
    pieces: List[str] = []
    _collect(paragraph, pieces)
    return "".join(pieces)


def _collect(element: ET.Element, out: List[str]) -> None:
    for child in element:
        tag = child.tag
        if tag in _DROPPED:
            continue
        if tag == _W + "t":
            out.append(child.text or "")
        elif tag in _MARKS:
            out.append(_MARKS[tag])
        elif tag == _W + "p":
            out.append("\n")
            _collect(child, out)
        elif tag == _ALTERNATE:
            _collect_first_choice(child, out)
        else:
            _collect(child, out)


def _collect_first_choice(alternate: ET.Element, out: List[str]) -> None:
    """``mc:AlternateContent`` repeats the same text in every alternative; keep one."""
    for choice in alternate:
        _collect(choice, out)
        return


# -- writing --------------------------------------------------------------------
def from_text(text: str) -> bytes:
    """Text → a minimal .docx with one paragraph per line; readable by :func:`to_text`."""
    cleaned = _XML_UNSAFE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    document = _document_xml(cleaned.split("\n"))
    parts: Tuple[Tuple[str, str], ...] = (
        ("[Content_Types].xml", CONTENT_TYPES),
        ("_rels/.rels", PACKAGE_RELS),
        (DOCUMENT_PART, document),
        ("word/_rels/document.xml.rels", DOCUMENT_RELS),
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts:
            info = zipfile.ZipInfo(name, _FIXED_STAMP)
            archive.writestr(info, content.encode("utf-8", errors="replace"), compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def _document_xml(lines: List[str]) -> str:
    body = "".join(_paragraph_xml(line) for line in lines)
    return f'{_XML_HEAD}<w:document xmlns:w="{W_NS}"><w:body>{body}<w:sectPr/></w:body></w:document>'


def _paragraph_xml(line: str) -> str:
    if not line:
        return "<w:p/>"
    runs = "<w:tab/>".join(_text_xml(part) for part in line.split("\t"))
    return f"<w:p><w:r>{runs}</w:r></w:p>"


def _text_xml(part: str) -> str:
    return f'<w:t xml:space="preserve">{escape(part)}</w:t>' if part else ""
