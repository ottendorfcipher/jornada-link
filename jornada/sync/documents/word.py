"""Backend "word": Microsoft Word documents in a OneDrive folder, through Microsoft Graph.

Documents are exchanged as .docx (see :mod:`docx`): Word Online opens what we
upload and we read what it saves; .txt files in the same folder are read too.
The folder is created on the first write when the listing found it missing.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from ...pim.models import Document
from ...pim.textfiles import decode_text, safe_filename
from ...webapi.http import HttpClient
from ...webapi.oauth import Provider, microsoft
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from . import docx
from .textcodec import DEVICE_FORMAT_SETTING, item_id_of, json_object, remote_modified, send

SERVICE = "OneDrive"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ("Files.ReadWrite", "offline_access")
DEFAULT_TENANT = "common"
DEFAULT_PATH = "Documents/Jornada"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_MIME = "text/plain; charset=utf-8"
LIST_SELECT = "id,name,file,lastModifiedDateTime,eTag,size"
EXTENSIONS = (".docx", ".txt")
CONFLICT_RENAME = "@microsoft.graph.conflictBehavior=rename"
HTTP_CONFLICT = 409
Log = Callable[[str], None]


class WordStore:
    """Store protocol over the .docx / .txt files of one OneDrive folder."""

    name = "word"

    def __init__(self, http: HttpClient, path: str = DEFAULT_PATH, log: Log = lambda _line: None) -> None:
        self._http = http
        self._path = path.strip("/ ")
        self._log = log
        self._folder_missing = False
        self._names: Dict[str, str] = {}   # item id → file name, from the last listing

    @property
    def path(self) -> str:
        return self._path

    def _url(self, suffix: str, child: str = "") -> str:
        """``/me/drive/root:/<path>[/<child>]:<suffix>`` (the plain root form for an empty path)."""
        parts = [p for p in (self._path, child) if p]
        if not parts:
            return f"/me/drive/root{suffix}"
        return f"/me/drive/root:/{quote('/'.join(parts), safe='/')}:{suffix}"

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        entries = self._children()
        self._names = {entry["id"]: str(entry.get("name", "")) for entry in entries}
        items = []
        for entry in entries:
            try:
                record = self._fetch(entry)
            except StoreError as exc:
                items.append(Item(id=entry["id"], record=None, problem=str(exc)))
                continue
            if record is not None:
                items.append(Item(id=entry["id"], record=record, version=entry.get("eTag")))
        return tuple(items)

    def create(self, record: Any) -> str:
        self._ensure_folder()
        name = safe_filename(record.name, ".docx")
        url = self._url("/content", name) + "?" + CONFLICT_RENAME
        response = send(self._http, SERVICE, "PUT", url, body=docx.from_text(record.text),
                        headers=(("Content-Type", DOCX_MIME),))
        payload = json_object(response, SERVICE, "upload")
        item_id = item_id_of(payload, SERVICE, "upload")
        self._names = {**self._names, item_id: str(payload.get("name") or name)}
        return item_id

    def update(self, item_id: str, record: Any) -> Optional[str]:
        as_text = self._names.get(item_id, "").lower().endswith(".txt")
        body = record.text.encode("utf-8") if as_text else docx.from_text(record.text)
        response = send(self._http, SERVICE, "PUT", f"/me/drive/items/{quote(item_id, safe='')}/content",
                        body=body, headers=(("Content-Type", TEXT_MIME if as_text else DOCX_MIME),))
        version = json_object(response, SERVICE, "upload").get("eTag")
        return str(version) if version else None

    def delete(self, item_id: str) -> None:
        response = send(self._http, SERVICE, "DELETE", f"/me/drive/items/{quote(item_id, safe='')}",
                        accept_errors=True)
        if response.status == 404:
            self._log(f"OneDrive item {item_id} was already gone")
        elif not response.ok:
            raise StoreError(f"{SERVICE} delete failed: HTTP {response.status} {response.text[:200].strip()}")
        self._names = {k: v for k, v in self._names.items() if k != item_id}

    # -- Graph calls ----------------------------------------------------------
    def _children(self) -> List[Dict[str, Any]]:
        url: Optional[str] = self._url("/children") + "?$select=" + LIST_SELECT
        found: List[Dict[str, Any]] = []
        while url:
            response = send(self._http, SERVICE, "GET", url, accept_errors=True)
            if response.status == 404 and not found:
                self._folder_missing = True
                self._log(f"OneDrive folder {self._path or '/'} does not exist yet")
                return []
            payload = json_object(response, SERVICE, "listing")
            found.extend(entry for entry in payload.get("value") or () if _is_document(entry))
            url = payload.get("@odata.nextLink") or None
        return found

    def _fetch(self, entry: Dict[str, Any]) -> Optional[Document]:
        name = str(entry.get("name", ""))
        response = send(self._http, SERVICE, "GET", f"/me/drive/items/{quote(entry['id'], safe='')}/content")
        if not response.ok:
            raise StoreError(f"{SERVICE} download of {name} failed: HTTP {response.status}")
        try:
            text = docx.to_text(response.body) if name.lower().endswith(".docx") else decode_text(response.body)
        except ValueError as exc:
            self._log(f"{name} cannot be read ({exc}); it is left alone")
            raise StoreError(f"{name}: {exc}") from exc
        return Document(name=_stem(name), text=text, kind="text",
                        modified=remote_modified(entry.get("lastModifiedDateTime")))

    def _ensure_folder(self) -> None:
        if not self._folder_missing:
            return
        prefix = ""
        for part in [p for p in self._path.split("/") if p]:
            self._create_folder(prefix, part)
            prefix = f"{prefix}/{part}" if prefix else part
        self._folder_missing = False
        self._log(f"created OneDrive folder {self._path}")

    def _create_folder(self, prefix: str, part: str) -> None:
        parent = f"/me/drive/root:/{quote(prefix, safe='/')}:/children" if prefix else "/me/drive/root/children"
        body = {"name": part, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        response = send(self._http, SERVICE, "POST", parent, json_body=body, accept_errors=True)
        if not response.ok and response.status != HTTP_CONFLICT:
            raise StoreError(f"{SERVICE} could not create folder {part!r}: HTTP {response.status} "
                             f"{response.text[:200].strip()}")


def _is_document(entry: Any) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or "file" not in entry:
        return False
    return str(entry.get("name", "")).lower().endswith(EXTENSIONS)


def _stem(name: str) -> str:
    stem, dot, _extension = name.rpartition(".")
    return stem if dot else name


# -- backend spec ---------------------------------------------------------------
def provider_for(account: Account) -> Provider:
    return microsoft((account.setting("tenant") or "").strip() or DEFAULT_TENANT)


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> WordStore:
    http = api_client(GRAPH_BASE, provider_for(account), SCOPES, account, secrets, context)
    return WordStore(http, account.setting("path") or DEFAULT_PATH, log=context.log)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(provider_for(account), SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key="word",
    title="Microsoft Word documents on OneDrive",
    settings=oauth_settings("Microsoft") + (
        SettingSpec("tenant", f"Azure AD tenant for sign-in (default {DEFAULT_TENANT})", required=False,
                    default=DEFAULT_TENANT),
        SettingSpec("path", f"OneDrive folder path (default {DEFAULT_PATH})", required=False, default=DEFAULT_PATH),
        DEVICE_FORMAT_SETTING,
    ),
    build=build,
    login=login,
    notes="Register a public client (mobile/desktop, redirect http://localhost) with the Files.ReadWrite "
          "permission; documents travel as plain paragraphs in .docx, and .txt files in the folder are read too.",
)
