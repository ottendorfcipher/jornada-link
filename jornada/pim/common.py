"""Helpers shared by the Pocket Outlook codecs."""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from ..cedb import CEVT_BLOB, CEVT_LPWSTR, PropVal, Record
from . import ids
from .notes_blob import decode_notes, encode_notes


def split_categories(text: Any) -> Tuple[str, ...]:
    if not isinstance(text, str):
        return ()
    return tuple(part.strip() for part in text.split(",") if part.strip())


def join_categories(categories: Tuple[str, ...]) -> str:
    return ",".join(c.strip() for c in categories if c.strip())


def string_of(record: Record, prop_id: int) -> str:
    value = record.value(prop_id, "")
    return value if isinstance(value, str) else ""


def int_of(record: Record, prop_id: int, default: int = 0) -> int:
    value = record.value(prop_id, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else (int(value) if isinstance(value, bool) else default)


def has_prop(existing: Optional[Record], prop_id: int) -> bool:
    return existing is not None and existing.get(prop_id) is not None


def put_string(props: List[PropVal], prop_id: int, value: str, existing: Optional[Record]) -> None:
    """Write a string property, or mark it deleted when it was set before and is now empty."""
    cleaned = (value or "").strip()
    if cleaned:
        props.append(PropVal.string(prop_id, cleaned))
    elif has_prop(existing, prop_id):
        props.append(PropVal.deleted(prop_id, CEVT_LPWSTR))


def put_notes(props: List[PropVal], prop_id: int, text: str, existing: Optional[Record]) -> None:
    if text.strip():
        props.append(PropVal.blob(prop_id, encode_notes(text)))
    elif has_prop(existing, prop_id):
        props.append(PropVal.deleted(prop_id, CEVT_BLOB))


def put_categories(props: List[PropVal], categories: Tuple[str, ...], existing: Optional[Record]) -> None:
    put_string(props, ids.CATEGORIES, join_categories(categories), existing)


def notes_of(record: Record) -> str:
    blob = record.value(ids.NOTES)
    if isinstance(blob, str):
        return blob.replace("\r\n", "\n")
    return decode_notes(blob if isinstance(blob, (bytes, bytearray)) else None)
