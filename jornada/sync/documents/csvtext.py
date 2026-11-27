"""CSV text is the neutral form of every sheet and table: a header row, then data rows.

Both sides of a sync produce their text through :func:`rows_to_csv` over
:func:`normalize_rows`, so a workbook, a Pocket Excel file, a SQLite table and
a device database that hold the same cells fingerprint the same. Cell values
are always text; :func:`parse_number` decides when text is *canonically* a
number ("7" and "1.5" are, "007" and "1.50" are not) so that typed storage
never loses what the user typed.
"""
from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime
from typing import Optional, Sequence, Tuple, Union

from ...pim.timeconv import parse_iso, to_wall_clock

Rows = Tuple[Tuple[str, ...], ...]
Number = Union[int, float]

COMMA = ","
TAB = "\t"
_BOM = "\ufeff"
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+(?:\.\d+)?(?:e[+-]?\d+)?$", re.IGNORECASE)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?$")
_INTEGRAL_LIMIT = 1e15


# -- delimited text --------------------------------------------------------------
def rows_to_delimited(rows: Sequence[Sequence[str]], delimiter: str) -> str:
    """Rows → text with LF line ends and minimal quoting."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    for row in rows:
        writer.writerow([str(cell) for cell in row])
    return buffer.getvalue()


def delimited_to_rows(text: str, delimiter: str) -> Rows:
    """Text → rows; tolerates CRLF line ends and a leading UTF-8 BOM. Malformed text raises ValueError."""
    cleaned = text[1:] if text.startswith(_BOM) else text
    try:
        return tuple(tuple(row) for row in csv.reader(io.StringIO(cleaned, newline=""), delimiter=delimiter))
    except csv.Error as exc:
        raise ValueError(f"malformed delimited text: {exc}") from exc


def rows_to_csv(rows: Sequence[Sequence[str]]) -> str:
    return rows_to_delimited(rows, COMMA)


def csv_to_rows(text: str) -> Rows:
    return delimited_to_rows(text, COMMA)


def rows_to_tsv(rows: Sequence[Sequence[str]]) -> str:
    return rows_to_delimited(rows, TAB)


def tsv_to_rows(text: str) -> Rows:
    return delimited_to_rows(text, TAB)


# -- shape ---------------------------------------------------------------------------
def _trim_row(row: Sequence[str]) -> Tuple[str, ...]:
    cells = tuple(str(cell) for cell in row)
    end = len(cells)
    while end and cells[end - 1] == "":
        end -= 1
    return cells[:end]


def normalize_rows(rows: Sequence[Sequence[str]]) -> Rows:
    """Drop trailing empty cells of every row and trailing empty rows (the canonical shape)."""
    trimmed = [_trim_row(row) for row in rows]
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    return tuple(trimmed)


def pad_rows(rows: Sequence[Sequence[str]], width: int) -> Rows:
    """Every row exactly ``width`` cells: short rows are padded, extra cells dropped."""
    return tuple(tuple(row[:width]) + ("",) * (width - len(row)) for row in rows)


def header_names(header: Sequence[str]) -> Tuple[str, ...]:
    """Column names that are non-empty and unique (case-insensitively): blanks become c0001…,
    duplicates get ``_2``, ``_3`` suffixes."""
    names: list = []
    for index, cell in enumerate(header):
        base = str(cell).strip() or f"c{index + 1:04d}"
        candidate, counter = base, 2
        while candidate.casefold() in {n.casefold() for n in names}:
            candidate = f"{base}_{counter}"
            counter += 1
        names.append(candidate)
    return tuple(names)


def table_from_csv(text: str) -> Tuple[Tuple[str, ...], Rows]:
    """CSV text → (column names, data rows padded to the header width). No header → ValueError."""
    rows = normalize_rows(csv_to_rows(text))
    if not rows or not rows[0]:
        raise ValueError("the sheet has no header row")
    columns = header_names(rows[0])
    return columns, pad_rows(rows[1:], len(columns))


# -- cell values ---------------------------------------------------------------------
def number_text(value: Number) -> str:
    """The canonical text of a number: integral values without a fraction, others as repr."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return str(value)
    if value == int(value) and abs(value) < _INTEGRAL_LIMIT:
        return str(int(value))
    return repr(value)


def parse_number(text: str) -> Optional[Number]:
    """The int or float a cell holds when its text is that number's canonical form, else None."""
    if _INT.match(text):
        value = int(text)
        return value if str(value) == text else None
    if not _FLOAT.match(text):
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and number_text(parsed) == text else None


def iso_to_naive(text: Optional[str]) -> Optional[datetime]:
    """ISO 8601 date/datetime text → naive local wall-clock datetime (None when it is not one)."""
    if not text or not _ISO.match(text.strip()):
        return None
    try:
        moment = parse_iso(text)
    except ValueError:
        return None
    if isinstance(moment, datetime):
        return to_wall_clock(moment)
    return datetime(moment.year, moment.month, moment.day)


def iso_cell_text(moment: datetime) -> str:
    """A datetime as cell text: the date alone at midnight, otherwise seconds precision."""
    if (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0):
        return moment.date().isoformat()
    return moment.replace(microsecond=0).isoformat(timespec="seconds")
