"""Just enough of the .xlsx package format to read one worksheet and to write one.

Reading follows the workbook's relationships to its first sheet part, resolves
shared and inline strings, and presents numbers as their canonical text.
Writing produces a minimal, valid package with inline strings; cells whose
text is canonically a number stay numeric. Only ``zipfile`` and
``xml.etree`` are used. Dates keep their serial numbers (styles are ignored).
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from typing import Dict, Iterable, Optional, Sequence, Tuple
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from .csvtext import Rows, normalize_rows, number_text, parse_number

WORKBOOK_PART = "xl/workbook.xml"
WORKBOOK_RELS_PART = "xl/_rels/workbook.xml.rels"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
SHEET_PART = "xl/worksheets/sheet1.xml"
STYLES_PART = "xl/styles.xml"
MAX_PART_SIZE = 64 * 1024 * 1024
MAX_CELLS = 2_000_000
DEFAULT_SHEET_NAME = "Sheet1"

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
_XML_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SHEET_NAME_UNSAFE = re.compile(r"[\[\]:*?/\\]")
_ZIP_STAMP = (1980, 1, 1, 0, 0, 0)

Element = ElementTree.Element


# -- cell references ---------------------------------------------------------------
def column_index(letters: str) -> int:
    """``"A"`` → 0, ``"Z"`` → 25, ``"AA"`` → 26."""
    index = 0
    for letter in letters.upper():
        if not "A" <= letter <= "Z":
            raise ValueError(f"bad column letters {letters!r}")
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index - 1


def column_letters(index: int) -> str:
    """0 → ``"A"``, 26 → ``"AA"``."""
    if index < 0:
        raise ValueError(f"bad column index {index}")
    letters = ""
    remaining = index + 1
    while remaining:
        remaining, digit = divmod(remaining - 1, 26)
        letters = chr(ord("A") + digit) + letters
    return letters


# -- reading ----------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_part(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise ValueError(f"the workbook has no {name} part") from None
    if info.file_size > MAX_PART_SIZE:
        raise ValueError(f"{name} is too large ({info.file_size} bytes)")
    return archive.read(name)


def _parse(data: bytes, what: str) -> Element:
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{what} is not well-formed XML: {exc}") from exc


def _first_sheet_part(archive: zipfile.ZipFile) -> str:
    workbook = _parse(_read_part(archive, WORKBOOK_PART), WORKBOOK_PART)
    sheet = next((e for e in workbook.iter() if _local(e.tag) == "sheet"), None)
    if sheet is None:
        raise ValueError("the workbook has no worksheets")
    rel_id = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
    rels = _parse(_read_part(archive, WORKBOOK_RELS_PART), WORKBOOK_RELS_PART)
    targets = {r.get("Id"): r.get("Target", "") for r in rels.iter() if _local(r.tag) == "Relationship"}
    target = targets.get(rel_id, "")
    if not target:
        raise ValueError(f"the workbook relationship {rel_id!r} has no target")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _rich_text(element: Element) -> str:
    """The text of an ``si``/``is`` element: plain ``t`` children and ``r`` runs (phonetics skipped)."""
    parts = []
    for child in element:
        name = _local(child.tag)
        if name == "t":
            parts.append(child.text or "")
        elif name == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return "".join(parts)


def _shared_strings(archive: zipfile.ZipFile) -> Tuple[str, ...]:
    if SHARED_STRINGS_PART not in archive.namelist():
        return ()
    root = _parse(_read_part(archive, SHARED_STRINGS_PART), SHARED_STRINGS_PART)
    return tuple(_rich_text(si) for si in root if _local(si.tag) == "si")


def _number_cell(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return ""
    try:
        return number_text(float(stripped))
    except ValueError:
        return stripped


def _cell_text(cell: Element, shared: Sequence[str]) -> str:
    kind = cell.get("t", "n")
    value = next((child for child in cell if _local(child.tag) == "v"), None)
    raw = (value.text or "") if value is not None else ""
    if kind == "s":
        index = int(raw.strip() or -1)
        if not 0 <= index < len(shared):
            raise ValueError(f"shared string {index} does not exist")
        return shared[index]
    if kind == "inlineStr":
        inline = next((child for child in cell if _local(child.tag) == "is"), None)
        return _rich_text(inline) if inline is not None else ""
    if kind == "b":
        return "TRUE" if raw.strip() == "1" else "FALSE"
    if kind in ("str", "e"):
        return raw
    return _number_cell(raw)


def _column_of(reference: Optional[str], default: int) -> int:
    """The 1-based column of a cell reference like ``"B7"``; ``default`` when the reference is absent."""
    if not reference:
        return default
    match = _CELL_REF.match(reference.strip().upper())
    if match is None:
        raise ValueError(f"bad cell reference {reference!r}")
    return column_index(match.group(1)) + 1


def _cells_of(sheet: Element, shared: Sequence[str]) -> Dict[int, Dict[int, str]]:
    cells: Dict[int, Dict[int, str]] = {}
    row_number = 0
    for row in sheet.iter():
        if _local(row.tag) != "row":
            continue
        row_number = int(row.get("r", row_number + 1))
        column = 0
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            column = _column_of(cell.get("r"), column + 1)
            cells.setdefault(row_number, {})[column] = _cell_text(cell, shared)
    return cells


def _grid(cells: Dict[int, Dict[int, str]]) -> Rows:
    if not cells:
        return ()
    height = max(cells)
    width = max((max(columns) for columns in cells.values() if columns), default=0)
    if height < 1 or width < 0 or height * width > MAX_CELLS:
        raise ValueError(f"the worksheet is too large ({height} rows × {width} columns)")
    return tuple(tuple(cells.get(r, {}).get(c, "") for c in range(1, width + 1)) for r in range(1, height + 1))


def to_rows(data: bytes) -> Rows:
    """The first worksheet of an .xlsx as canonical rows (see :func:`csvtext.normalize_rows`)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            part = _first_sheet_part(archive)
            shared = _shared_strings(archive)
            sheet = _parse(_read_part(archive, part), part)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not an .xlsx package: {exc}") from exc
    return normalize_rows(_grid(_cells_of(sheet, shared)))


# -- writing ----------------------------------------------------------------------------
def _cell_xml(row: int, column: int, text: str) -> str:
    reference = f"{column_letters(column - 1)}{row}"
    number = parse_number(text)
    if number is not None:
        return f'<c r="{reference}"><v>{number_text(number)}</v></c>'
    clean = escape(_XML_UNSAFE.sub("", text))
    return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{clean}</t></is></c>'


def _sheet_xml(rows: Iterable[Sequence[str]]) -> str:
    lines = []
    for row_number, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(row_number, column, text) for column, text in enumerate(row, start=1) if text != "")
        if cells:
            lines.append(f'<row r="{row_number}">{cells}</row>')
    return f'{_XML_HEADER}<worksheet xmlns="{_NS_MAIN}"><sheetData>{"".join(lines)}</sheetData></worksheet>'


def _sheet_name(name: str) -> str:
    cleaned = _SHEET_NAME_UNSAFE.sub("_", name).strip()[:31]
    return escape(cleaned or DEFAULT_SHEET_NAME, {'"': "&quot;"})


def _package_parts(rows: Sequence[Sequence[str]], sheet_name: str) -> Tuple[Tuple[str, str], ...]:
    content_types = (
        f'{_XML_HEADER}<Types xmlns="{_NS_TYPES}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/{WORKBOOK_PART}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'<Override PartName="/{SHEET_PART}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        f'<Override PartName="/{STYLES_PART}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        f'{_XML_HEADER}<Relationships xmlns="{_NS_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="{WORKBOOK_PART}"/>'
        "</Relationships>"
    )
    workbook = (
        f'{_XML_HEADER}<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
        f'<sheets><sheet name="{_sheet_name(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        f'{_XML_HEADER}<Relationships xmlns="{_NS_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_NS_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{_NS_REL}/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles = (
        f'{_XML_HEADER}<styleSheet xmlns="{_NS_MAIN}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    return (("[Content_Types].xml", content_types), ("_rels/.rels", root_rels), (WORKBOOK_PART, workbook),
            (WORKBOOK_RELS_PART, workbook_rels), (STYLES_PART, styles), (SHEET_PART, _sheet_xml(rows)))


def from_rows(rows: Sequence[Sequence[str]], sheet_name: str = DEFAULT_SHEET_NAME) -> bytes:
    """A minimal .xlsx holding ``rows`` on one worksheet (deterministic bytes for equal input)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in _package_parts(rows, sheet_name):
            info = zipfile.ZipInfo(name, date_time=_ZIP_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode("utf-8"))
    return buffer.getvalue()
