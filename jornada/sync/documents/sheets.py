"""Google Sheets: the spreadsheets of one Drive folder, each read as its first sheet.

Drive v3 lists, moves and deletes the files; Sheets v4 reads and writes the
cell values (``FORMATTED_VALUE`` so the text matches what the user sees,
``USER_ENTERED`` so numbers and dates typed on the Jornada become real cells).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from ...pim.models import Document
from ...webapi.http import HttpClient, HttpError, HttpResponse
from ...webapi.oauth import GOOGLE
from ...webapi.oauth_accounts import authorization_provider, login_account, oauth_settings
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .csvtext import Rows, csv_to_rows, iso_to_naive, normalize_rows, number_text, rows_to_csv
from .sheetcodec import KIND

KEY = "sheets"
DRIVE_URL = "https://www.googleapis.com/drive/v3"
SHEETS_URL = "https://sheets.googleapis.com/v4"
SCOPES = ("https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/spreadsheets")
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_FOLDER_NAME = "Jornada"
RANGE = "A1:ZZ10000"
PAGE_SIZE = 100
FILE_FIELDS = "nextPageToken,files(id,name,modifiedTime,version)"
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

SETTINGS = oauth_settings("Google") + (
    SettingSpec("folder_id", "Drive folder id holding the spreadsheets (found by name when unset)",
                required=False),
    SettingSpec("folder_name", f"Drive folder name, found or created under My Drive (default {DEFAULT_FOLDER_NAME})",
                required=False, default=DEFAULT_FOLDER_NAME),
)


def _quoted(value: str) -> str:
    """A string literal for a Drive ``q`` query."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return number_text(value)
    return str(value)


def _object(response: HttpResponse) -> Dict[str, Any]:
    try:
        payload = response.json()
    except HttpError as exc:
        raise StoreError(f"Google: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


class SheetsStore:
    """Store protocol over the spreadsheets of one Drive folder."""

    name = KEY

    def __init__(self, drive: HttpClient, sheets: HttpClient, folder_id: Optional[str] = None,
                 folder_name: str = DEFAULT_FOLDER_NAME, log: Callable[[str], None] = lambda _line: None) -> None:
        self._drive = drive
        self._sheets = sheets
        self._folder_id = folder_id
        self._folder_name = folder_name.strip() or DEFAULT_FOLDER_NAME
        self._log = log

    # -- API plumbing ---------------------------------------------------------
    @staticmethod
    def _call(client: HttpClient, method: str, path: str, **kwargs: Any) -> HttpResponse:
        try:
            return client.request(method, path, **kwargs)
        except HttpError as exc:
            raise StoreError(f"Google: {exc}") from exc

    def _folder(self) -> str:
        if self._folder_id:
            return self._folder_id
        query = (f"name = {_quoted(self._folder_name)} and mimeType = {_quoted(FOLDER_MIME)} "
                 "and 'root' in parents and trashed = false")
        found = _object(self._call(self._drive, "GET", "/files", params={"q": query, "fields": "files(id)"}))
        files = found.get("files") or []
        if files and files[0].get("id"):
            self._folder_id = str(files[0]["id"])
            return self._folder_id
        created = _object(self._call(self._drive, "POST", "/files", params={"fields": "id"},
                                     json_body={"name": self._folder_name, "mimeType": FOLDER_MIME, "parents": ["root"]}))
        if not created.get("id"):
            raise StoreError(f"Google: creating the Drive folder {self._folder_name!r} returned no id")
        self._folder_id = str(created["id"])
        self._log(f"created Drive folder {self._folder_name}")
        return self._folder_id

    def _files(self) -> List[Dict[str, Any]]:
        query = (f"{_quoted(self._folder())} in parents and mimeType = {_quoted(SPREADSHEET_MIME)} "
                 "and trashed = false")
        files: List[Dict[str, Any]] = []
        token: Optional[str] = None
        while True:
            params = {"q": query, "fields": FILE_FIELDS, "pageSize": PAGE_SIZE, "orderBy": "name", "pageToken": token}
            payload = _object(self._call(self._drive, "GET", "/files", params=params))
            files.extend(payload.get("files") or [])
            token = payload.get("nextPageToken")
            if not token:
                return files

    def _values(self, spreadsheet_id: str) -> Rows:
        path = f"/spreadsheets/{quote(spreadsheet_id, safe='')}/values/{RANGE}"
        payload = _object(self._call(self._sheets, "GET", path, params={"valueRenderOption": "FORMATTED_VALUE"}))
        values = payload.get("values") or []
        return normalize_rows(tuple(tuple(_cell_text(cell) for cell in row) for row in values))

    def _write_values(self, spreadsheet_id: str, rows: Rows) -> None:
        if not rows:
            return
        path = f"/spreadsheets/{quote(spreadsheet_id, safe='')}/values/{RANGE}"
        body = {"range": RANGE, "majorDimension": "ROWS", "values": [list(row) for row in rows]}
        self._call(self._sheets, "PUT", path, params={"valueInputOption": "USER_ENTERED"}, json_body=body)

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        items = []
        for entry in self._files():
            if not entry.get("id"):
                continue
            file_id = str(entry["id"])
            record = Document(name=str(entry.get("name") or ""), text=rows_to_csv(self._values(file_id)), kind=KIND,
                              modified=iso_to_naive(entry.get("modifiedTime")))
            version = entry.get("version")
            items.append(Item(id=file_id, record=record, version=str(version) if version is not None else None))
        return tuple(items)

    def create(self, record: Document) -> str:
        rows = normalize_rows(csv_to_rows(record.text))
        created = _object(self._call(self._sheets, "POST", "/spreadsheets",
                                     json_body={"properties": {"title": record.name.strip() or "Untitled"}}))
        if not created.get("spreadsheetId"):
            raise StoreError("Google: creating the spreadsheet returned no id")
        spreadsheet_id = str(created["spreadsheetId"])
        self._call(self._drive, "PATCH", f"/files/{quote(spreadsheet_id, safe='')}",
                   params={"addParents": self._folder(), "removeParents": "root", "fields": "id,parents"}, json_body={})
        self._write_values(spreadsheet_id, rows)
        return spreadsheet_id

    def update(self, item_id: str, record: Document) -> Optional[str]:
        rows = normalize_rows(csv_to_rows(record.text))
        path = f"/spreadsheets/{quote(item_id, safe='')}/values/{RANGE}:clear"
        self._call(self._sheets, "POST", path, json_body={})
        self._write_values(item_id, rows)
        return None

    def delete(self, item_id: str) -> None:
        self._call(self._drive, "DELETE", f"/files/{quote(item_id, safe='')}")


# -- backend --------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> SheetsStore:
    folder_id = (account.setting("folder_id") or "").strip()
    if folder_id and not _FILE_ID.match(folder_id):
        raise AccountError(f"folder_id {folder_id!r} is not a Drive file id")
    auth = authorization_provider(GOOGLE, SCOPES, account, secrets, context)
    drive = HttpClient(DRIVE_URL, transport=context.http_transport, auth=auth)
    sheets = HttpClient(SHEETS_URL, transport=context.http_transport, auth=auth)
    return SheetsStore(drive, sheets, folder_id or None, account.setting("folder_name") or DEFAULT_FOLDER_NAME,
                       log=context.log)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(GOOGLE, SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key=KEY,
    title="Google Sheets",
    settings=SETTINGS,
    build=build,
    login=login,
    notes="Each spreadsheet's first sheet is synced as one CSV sheet.",
)
