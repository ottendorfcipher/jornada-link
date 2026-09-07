"""The device side of the spreadsheet backends: a folder of delimited text files.

Pocket Excel opens comma-separated ``.csv`` and tab-separated ``.txt`` files;
its binary ``.pxl`` workbooks are not converted. Files are decoded through
:mod:`jornada.pim.textfiles` (ANSI/UTF-8/UTF-16 aware) and re-canonicalized
as CSV so the device and the modern side fingerprint the same cells.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional, Tuple

from ...pim.filestore import DeviceFolderStore, FileCodec
from ...pim.models import Document
from ...pim.textfiles import decode_text, encode_text, safe_filename
from ...rapi import RapiClient
from ..accounts import Account, AccountError
from ..registry import BuildContext, SettingSpec
from .csvtext import csv_to_rows, normalize_rows, rows_to_csv, rows_to_tsv, tsv_to_rows

DEVICE_FOLDER = "\\My Documents"
FOLDER_SETTING = "folder"
FORMAT_SETTING = "device_format"
FORMAT_CSV = "csv"
FORMAT_TSV = "tsv"
CSV_EXTENSION = ".csv"
TSV_EXTENSION = ".txt"
KIND = "sheet"
DEVICE_FORMAT_SETTING = SettingSpec(
    FORMAT_SETTING, f"file type written to the device: {FORMAT_CSV} (default) or {FORMAT_TSV} "
                    "(tab-separated .txt, which Pocket Excel opens directly)", required=False, default=FORMAT_CSV)


def _stamp(mtime: Optional[float]) -> Optional[datetime]:
    if not mtime:
        return None
    try:
        return datetime.fromtimestamp(mtime)
    except (OverflowError, OSError, ValueError):
        return None


def decode_sheet(name: str, data: bytes, mtime: Optional[float]) -> Document:
    """A device file → ``Document(kind="sheet")`` whose text is canonical CSV."""
    stem, dot, _ext = name.rpartition(".")
    text = decode_text(data)
    rows = tsv_to_rows(text) if name.lower().endswith(TSV_EXTENSION) else csv_to_rows(text)
    return Document(name=stem if dot else name, text=rows_to_csv(normalize_rows(rows)), kind=KIND,
                    modified=_stamp(mtime))


def encode_sheet(document: Document, device_format: str = FORMAT_CSV) -> Tuple[str, bytes]:
    """A document → (device file name, ANSI/CRLF bytes) in the account's device format."""
    rows = normalize_rows(csv_to_rows(document.text))
    if device_format == FORMAT_TSV:
        return safe_filename(document.name, TSV_EXTENSION), encode_text(rows_to_tsv(rows))
    return safe_filename(document.name, CSV_EXTENSION), encode_text(rows_to_csv(rows))


def sheet_codec(device_format: str = FORMAT_CSV) -> FileCodec:
    """The folder codec: ``.csv`` always, ``.txt`` (tab-separated) too when writing TSV."""
    extensions = (CSV_EXTENSION, TSV_EXTENSION) if device_format == FORMAT_TSV else (CSV_EXTENSION,)
    encode: Callable[[Document], Tuple[str, bytes]] = lambda document: encode_sheet(document, device_format)
    return FileCodec(extensions=extensions, decode=decode_sheet, encode=encode)


def device_format_of(account: Account) -> str:
    value = (account.setting(FORMAT_SETTING) or FORMAT_CSV).strip().lower()
    if value not in (FORMAT_CSV, FORMAT_TSV):
        raise AccountError(f"{FORMAT_SETTING} must be {FORMAT_CSV!r} or {FORMAT_TSV!r}, not {value!r}")
    return value


def device_folder_store(client: RapiClient, account: Account, context: BuildContext) -> DeviceFolderStore:
    """The store over ``account.setting("folder")`` (default ``\\My Documents``)."""
    folder = (account.setting(FOLDER_SETTING) or "").strip() or DEVICE_FOLDER
    return DeviceFolderStore(client, folder, sheet_codec(device_format_of(account)), log=context.log)
