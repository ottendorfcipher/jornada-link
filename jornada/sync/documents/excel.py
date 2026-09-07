"""Excel workbooks (and .csv files) in a OneDrive folder, through Microsoft Graph.

Each workbook is one ``Document(kind="sheet")`` made of its first worksheet.
Downloads prefer the item's pre-authenticated ``@microsoft.graph.downloadUrl``
(fetched without the bearer token, as Graph asks) and fall back to
``/content``; uploads are simple ``PUT /content`` requests, fine for the
small sheets a Pocket Excel user keeps.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from ...pim.models import Document
from ...pim.textfiles import decode_text, safe_filename
from ...webapi.http import HttpClient, HttpError, HttpResponse
from ...webapi.oauth import Provider, microsoft
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .csvtext import Rows, csv_to_rows, iso_to_naive, normalize_rows, rows_to_csv
from .sheetcodec import DEVICE_FORMAT_SETTING, KIND
from .xlsx import from_rows, to_rows

KEY = "excel"
GRAPH_URL = "https://graph.microsoft.com/v1.0"
SCOPES = ("Files.ReadWrite", "offline_access")
DEFAULT_TENANT = "common"
DEFAULT_PATH = "Documents/Jornada"
XLSX = ".xlsx"
CSV = ".csv"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"
PAGE_SIZE = 200
DOWNLOAD_URL = "@microsoft.graph.downloadUrl"
CONFLICT = "@microsoft.graph.conflictBehavior"

SETTINGS = oauth_settings("Microsoft") + (
    SettingSpec("tenant", f"Azure AD tenant of the app registration (default {DEFAULT_TENANT})",
                required=False, default=DEFAULT_TENANT),
    SettingSpec("path", f"OneDrive folder holding the workbooks (default {DEFAULT_PATH})",
                required=False, default=DEFAULT_PATH),
    DEVICE_FORMAT_SETTING,
)


def provider_for(account: Account) -> Provider:
    return microsoft((account.setting("tenant") or "").strip() or DEFAULT_TENANT)


def _extension(name: str) -> str:
    lowered = name.lower()
    return next((ext for ext in (XLSX, CSV) if lowered.endswith(ext)), "")


def _csv_bytes(rows: Rows) -> bytes:
    """CSV as Excel likes it: UTF-8 with a BOM and CRLF line ends."""
    return ("\ufeff" + rows_to_csv(rows).replace("\n", "\r\n")).encode("utf-8")


class ExcelStore:
    """Store protocol over the .xlsx and .csv items of one OneDrive folder."""

    name = KEY

    def __init__(self, http: HttpClient, path: str = DEFAULT_PATH, log: Callable[[str], None] = lambda _line: None,
                 downloads: Optional[HttpClient] = None) -> None:
        self._http = http
        self._downloads = downloads
        self._path = path.strip().strip("/") or DEFAULT_PATH
        self._log = log
        self._extensions: Dict[str, str] = {}

    # -- Graph plumbing -------------------------------------------------------
    def _call(self, method: str, path: str, **kwargs: Any) -> HttpResponse:
        try:
            return self._http.request(method, path, **kwargs)
        except HttpError as exc:
            raise StoreError(f"OneDrive: {exc}") from exc

    def _item_path(self, name: Optional[str] = None) -> str:
        base = "/me/drive/root:/" + quote(self._path, safe="/")
        return f"{base}/{quote(name, safe='')}:" if name else base + ":"

    def _children(self) -> List[Dict[str, Any]]:
        response = self._call("GET", self._item_path() + "/children", params={"$top": PAGE_SIZE}, accept_errors=True)
        if response.status == 404:
            self._create_folder()
            return []
        if not response.ok:
            raise StoreError(f"OneDrive: listing {self._path} failed (HTTP {response.status})")
        payload = _object(response)
        entries = list(payload.get("value") or [])
        while payload.get("@odata.nextLink"):
            payload = _object(self._call("GET", str(payload["@odata.nextLink"])))
            entries.extend(payload.get("value") or [])
        return entries

    def _create_folder(self) -> None:
        parts = [part for part in self._path.split("/") if part]
        for index, part in enumerate(parts):
            parent = "/".join(parts[:index])
            parent_path = "/me/drive/root:/" + quote(parent, safe="/") + ":" if parent else "/me/drive/root"
            response = self._call("POST", parent_path + "/children", accept_errors=True,
                                  json_body={"name": part, "folder": {}, CONFLICT: "fail"})
            if response.status not in (200, 201, 409):
                raise StoreError(f"OneDrive: cannot create folder {part!r} (HTTP {response.status})")
        self._log(f"created OneDrive folder {self._path}")

    def _content(self, entry: Dict[str, Any]) -> bytes:
        url = entry.get(DOWNLOAD_URL)
        if url and self._downloads is not None:
            try:
                return self._downloads.get(str(url)).body
            except HttpError as exc:
                raise StoreError(f"OneDrive: download of {entry.get('name')!r} failed ({exc.status})") from exc
        return self._call("GET", f"/me/drive/items/{quote(str(entry['id']), safe='')}/content").body

    def _rows_of(self, entry: Dict[str, Any], extension: str) -> Rows:
        data = self._content(entry)
        if extension == XLSX:
            return to_rows(data)
        return normalize_rows(csv_to_rows(decode_text(data)))

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        items = []
        for entry in self._children():
            name = str(entry.get("name") or "")
            extension = _extension(name)
            if not extension or "folder" in entry or "id" not in entry:
                continue
            try:
                rows = self._rows_of(entry, extension)
            except ValueError as exc:
                self._log(f"{name} cannot be read ({exc}); it is left alone")
                items.append(Item(id=str(entry["id"]), record=None, problem=str(exc)))
                continue
            item_id = str(entry["id"])
            self._extensions[item_id] = extension
            record = Document(name=name[:-len(extension)], text=rows_to_csv(rows), kind=KIND,
                              modified=iso_to_naive(entry.get("lastModifiedDateTime")))
            items.append(Item(id=item_id, record=record, version=entry.get("eTag")))
        return tuple(items)

    def create(self, record: Document) -> str:
        rows = normalize_rows(csv_to_rows(record.text))
        name = safe_filename(record.name, XLSX)
        response = self._call("PUT", self._item_path(name) + "/content", params={CONFLICT: "rename"},
                              headers=(("Content-Type", XLSX_MIME),), body=from_rows(rows))
        created = _object(response)
        if "id" not in created:
            raise StoreError(f"OneDrive: upload of {name!r} returned no item id")
        item_id = str(created["id"])
        self._extensions[item_id] = XLSX
        return item_id

    def update(self, item_id: str, record: Document) -> Optional[str]:
        rows = normalize_rows(csv_to_rows(record.text))
        extension = self._extensions.get(item_id, XLSX)
        body = _csv_bytes(rows) if extension == CSV else from_rows(rows)
        mime = CSV_MIME if extension == CSV else XLSX_MIME
        response = self._call("PUT", f"/me/drive/items/{quote(item_id, safe='')}/content",
                              headers=(("Content-Type", mime),), body=body)
        return _object(response).get("eTag")

    def delete(self, item_id: str) -> None:
        self._call("DELETE", f"/me/drive/items/{quote(item_id, safe='')}")
        self._extensions.pop(item_id, None)


def _object(response: HttpResponse) -> Dict[str, Any]:
    try:
        payload = response.json()
    except HttpError as exc:
        raise StoreError(f"OneDrive: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


# -- backend --------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> ExcelStore:
    http = api_client(GRAPH_URL, provider_for(account), SCOPES, account, secrets, context)
    downloads = HttpClient(transport=context.http_transport)
    return ExcelStore(http, account.setting("path") or DEFAULT_PATH, log=context.log, downloads=downloads)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(provider_for(account), SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key=KEY,
    title="Microsoft Excel workbooks on OneDrive",
    settings=SETTINGS,
    build=build,
    login=login,
    notes="Each workbook's first worksheet is synced; .csv files in the folder are synced too.",
)
