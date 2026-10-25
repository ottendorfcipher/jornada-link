"""The device side of text-document sync — Pocket Word's .txt / .rtf files as ``Document``
records — plus the few pieces the three text backends share: the ``device_format``
setting, remote time stamps and the HTTP-error-to-StoreError translation.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from ...pim.filestore import DeviceFolderStore, FileCodec
from ...pim.models import Document
from ...pim.textfiles import decode_text, encode_text, safe_filename
from ...pim.timeconv import parse_iso, to_wall_clock
from ...rapi import RapiClient
from ...webapi.http import HttpClient, HttpError, HttpResponse
from ..accounts import Account, AccountError
from ..base import StoreError
from ..registry import BuildContext, SettingSpec
from . import rtf

TEXT_EXTENSIONS = (".txt", ".rtf")
DEVICE_FORMATS = ("txt", "rtf")
DEFAULT_DEVICE_FORMAT = "txt"
DEFAULT_DEVICE_FOLDER = "\\My Documents"
DEVICE_FORMAT_SETTING = SettingSpec(
    "device_format", "file type written to the device: txt (default, Pocket Word ANSI text) or rtf",
    required=False, default=DEFAULT_DEVICE_FORMAT,
)
Log = Callable[[str], None]
_FRACTION = re.compile(r"\.\d+")


# -- the codec ------------------------------------------------------------------
def decode(name: str, data: bytes, mtime: Optional[float]) -> Document:
    """A device file → ``Document(name=stem, text)``; .rtf through the RTF reader, else text."""
    stem, dot, extension = name.rpartition(".")
    text = rtf.to_text(data) if dot and extension.lower() == "rtf" else decode_text(data)
    modified = datetime.fromtimestamp(mtime) if mtime else None
    return Document(name=stem if dot else name, text=text, kind="text", modified=modified)


def encode(record: Any, device_format: str = DEFAULT_DEVICE_FORMAT) -> Tuple[str, bytes]:
    """``Document`` → (device file name, bytes) in the account's ``device_format``."""
    if device_format == "rtf":
        return safe_filename(record.name, ".rtf"), rtf.from_text(record.text)
    return safe_filename(record.name, ".txt"), encode_text(record.text)


def codec_for(device_format: str = DEFAULT_DEVICE_FORMAT) -> FileCodec:
    if device_format not in DEVICE_FORMATS:
        raise AccountError(f"device_format must be one of {', '.join(DEVICE_FORMATS)}, not {device_format!r}")
    return FileCodec(extensions=TEXT_EXTENSIONS, decode=decode,
                     encode=lambda record: encode(record, device_format))


def device_format_of(account: Account) -> str:
    return (account.setting("device_format") or DEFAULT_DEVICE_FORMAT).strip().lower()


def device_store(client: RapiClient, account: Account, context: BuildContext) -> DeviceFolderStore:
    """The device folder (setting ``folder``, default ``\\My Documents``) as a Store."""
    codec = codec_for(device_format_of(account))
    folder = account.setting("folder") or DEFAULT_DEVICE_FOLDER
    return DeviceFolderStore(client, folder, codec, source=f"sync:{account.name}", log=context.log)


# -- shared by the backends -----------------------------------------------------
def remote_modified(value: Any) -> Optional[datetime]:
    """An ISO 8601 stamp from a web API → naive local wall-clock; None when absent or odd."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = parse_iso(_FRACTION.sub("", value))
    except ValueError:
        return None
    return to_wall_clock(moment) if isinstance(moment, datetime) else None


def send(http: HttpClient, service: str, method: str, url: str, **kwargs: Any) -> HttpResponse:
    """One request; a transport failure or HTTP error becomes a StoreError naming ``service``."""
    try:
        return http.request(method, url, **kwargs)
    except HttpError as exc:
        raise StoreError(f"{service}: {exc}") from exc


def json_object(response: HttpResponse, service: str, what: str) -> Dict[str, Any]:
    """The JSON object of a successful response; anything else is a StoreError."""
    if not response.ok:
        raise StoreError(f"{service} {what} failed: HTTP {response.status} {response.text[:200].strip()}")
    try:
        payload = response.json()
    except HttpError as exc:
        raise StoreError(f"{service} {what}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StoreError(f"{service} {what}: unexpected response {str(payload)[:80]!r}")
    return payload


def item_id_of(payload: Dict[str, Any], service: str, what: str) -> str:
    item_id = payload.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise StoreError(f"{service} {what}: the response carries no item id")
    return item_id
