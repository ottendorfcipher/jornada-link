"""Backend "gdocs": Google Docs in one Drive folder, through the Drive v3 API.

Text goes up as ``text/plain`` and Drive converts it into a Doc; it comes back
through the Doc's plain-text export. The ``drive.file`` scope only reveals files
this tool created (or the user opened with it), so the sync folder is one the
tool makes itself unless ``folder_id`` points at a folder it can see.
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from ...pim.models import Document
from ...pim.textfiles import decode_text
from ...webapi.http import HttpClient
from ...webapi.oauth import GOOGLE
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .textcodec import DEVICE_FORMAT_SETTING, item_id_of, json_object, remote_modified, send

SERVICE = "Google Drive"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
SCOPES = ("https://www.googleapis.com/auth/drive.file",)
DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
TEXT_MIME = "text/plain; charset=UTF-8"
DEFAULT_FOLDER_NAME = "Jornada"
LIST_FIELDS = "nextPageToken,files(id,name,modifiedTime,version)"
PAGE_SIZE = 100
Log = Callable[[str], None]


def new_boundary() -> str:
    return "jornada-" + secrets.token_hex(12)


def multipart_related(boundary: str, metadata: Dict[str, Any], text: str) -> bytes:
    """A two-part ``multipart/related`` body: JSON metadata, then the document as UTF-8 text."""
    delimiter = f"--{boundary}\r\n".encode("ascii")
    parts = (
        delimiter + b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata).encode("utf-8") + b"\r\n",
        delimiter + b"Content-Type: text/plain; charset=UTF-8\r\n\r\n" + text.encode("utf-8") + b"\r\n",
    )
    return b"".join(parts) + f"--{boundary}--\r\n".encode("ascii")


def query_literal(value: str) -> str:
    """A string literal for Drive's ``q`` language (backslash and quote escaped)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDocsStore:
    """Store protocol over the Google Docs of one Drive folder."""

    name = "gdocs"

    def __init__(self, http: HttpClient, folder_id: Optional[str] = None, folder_name: str = DEFAULT_FOLDER_NAME,
                 log: Log = lambda _line: None, boundary: Callable[[], str] = new_boundary) -> None:
        self._http = http
        self._folder_id = (folder_id or "").strip() or None
        self._folder_name = folder_name.strip() or DEFAULT_FOLDER_NAME
        self._log = log
        self._boundary = boundary

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        folder = self._folder()
        query = f"'{query_literal(folder)}' in parents and mimeType='{DOC_MIME}' and trashed=false"
        items: List[Item] = []
        token: Optional[str] = None
        while True:
            params = {"q": query, "fields": LIST_FIELDS, "pageSize": PAGE_SIZE, "pageToken": token}
            payload = self._json("GET", "/files", "listing", params=params)
            items.extend(self._item(entry) for entry in payload.get("files") or () if _is_file(entry))
            token = payload.get("nextPageToken") or None
            if not token:
                return tuple(items)

    def create(self, record: Any) -> str:
        metadata = {"name": record.name or "Untitled", "mimeType": DOC_MIME, "parents": [self._folder()]}
        boundary = self._boundary()
        payload = self._json("POST", f"{UPLOAD_BASE}/files", "upload",
                             params={"uploadType": "multipart", "fields": "id,version"},
                             body=multipart_related(boundary, metadata, record.text),
                             headers=(("Content-Type", f"multipart/related; boundary={boundary}"),))
        return item_id_of(payload, SERVICE, "upload")

    def update(self, item_id: str, record: Any) -> Optional[str]:
        payload = self._json("PATCH", f"{UPLOAD_BASE}/files/{quote(item_id, safe='')}", "update",
                             params={"uploadType": "media", "fields": "id,version"},
                             body=record.text.encode("utf-8"), headers=(("Content-Type", TEXT_MIME),))
        return _version(payload)

    def delete(self, item_id: str) -> None:
        # Trash rather than DELETE: a mistaken deletion can be undone from Drive's Trash.
        response = send(self._http, SERVICE, "PATCH", f"/files/{quote(item_id, safe='')}",
                        json_body={"trashed": True}, accept_errors=True)
        if response.status == 404:
            self._log(f"Drive file {item_id} was already gone")
        elif not response.ok:
            raise StoreError(f"{SERVICE} delete failed: HTTP {response.status} {response.text[:200].strip()}")

    # -- Drive calls ----------------------------------------------------------
    def _json(self, method: str, url: str, what: str, **kwargs: Any) -> Dict[str, Any]:
        return json_object(send(self._http, SERVICE, method, url, **kwargs), SERVICE, what)

    def _folder(self) -> str:
        if self._folder_id is None:
            self._folder_id = self._find_folder() or self._create_folder()
        return self._folder_id

    def _find_folder(self) -> Optional[str]:
        query = (f"name='{query_literal(self._folder_name)}' and mimeType='{FOLDER_MIME}' "
                 "and 'root' in parents and trashed=false")
        payload = self._json("GET", "/files", "folder lookup", params={"q": query, "fields": "files(id,name)"})
        for entry in payload.get("files") or ():
            if _is_file(entry):
                return str(entry["id"])
        return None

    def _create_folder(self) -> str:
        payload = self._json("POST", "/files", "folder creation", params={"fields": "id"},
                             json_body={"name": self._folder_name, "mimeType": FOLDER_MIME})
        self._log(f"created Drive folder {self._folder_name}")
        return item_id_of(payload, SERVICE, "folder creation")

    def _item(self, entry: Dict[str, Any]) -> Item:
        file_id = str(entry["id"])
        response = send(self._http, SERVICE, "GET", f"/files/{quote(file_id, safe='')}/export",
                        params={"mimeType": "text/plain"})
        if not response.ok:
            raise StoreError(f"{SERVICE} export of {entry.get('name')!r} failed: HTTP {response.status}")
        record = Document(name=str(entry.get("name") or ""), text=decode_text(response.body), kind="text",
                          modified=remote_modified(entry.get("modifiedTime")))
        return Item(id=file_id, record=record, version=_version(entry))


def _is_file(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("id"), str) and bool(entry["id"])


def _version(payload: Dict[str, Any]) -> Optional[str]:
    version = payload.get("version")
    return str(version) if version not in (None, "") else None


# -- backend spec ---------------------------------------------------------------
def build(account: Account, secrets_: Dict[str, Any], context: BuildContext) -> GoogleDocsStore:
    http = api_client(DRIVE_BASE, GOOGLE, SCOPES, account, secrets_, context)
    return GoogleDocsStore(http, account.setting("folder_id"), account.setting("folder_name") or DEFAULT_FOLDER_NAME,
                           log=context.log)


def login(account: Account, secrets_: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(GOOGLE, SCOPES, account, secrets_, context)


BACKEND = BackendSpec(
    key="gdocs",
    title="Google Docs",
    settings=oauth_settings("Google") + (
        SettingSpec("folder_id", "Drive folder id to sync (default: a folder named folder_name under My Drive)",
                    required=False),
        SettingSpec("folder_name", f"name of the Drive folder to find or create (default {DEFAULT_FOLDER_NAME})",
                    required=False, default=DEFAULT_FOLDER_NAME),
        DEVICE_FORMAT_SETTING,
    ),
    build=build,
    login=login,
    notes="Uses the drive.file scope, which only sees files this tool created or that you opened with it: "
          "Docs written elsewhere in Drive stay invisible until they are recreated through the sync. "
          "Documents are exchanged as plain text (Drive converts uploads into Docs).",
)
