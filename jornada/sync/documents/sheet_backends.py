"""The spreadsheet and table family of the documents module.

Excel, Google Sheets and Numbers keep a device folder of ``.csv`` files in
step (Pocket Excel); the SQLite backend keeps object-store databases in step
(Pocket Access), so the device store depends on the account's backend.
"""
from __future__ import annotations

from typing import Tuple

from ...rapi import RapiClient
from ..accounts import Account
from ..base import Store
from ..registry import BackendSpec, BuildContext
from .excel import BACKEND as EXCEL
from .numbers import BACKEND as NUMBERS
from .sheetcodec import device_folder_store
from .sheets import BACKEND as SHEETS
from .sqlite_tables import BACKEND as SQLITE
from .tablestore import device_table_store

BACKENDS: Tuple[BackendSpec, ...] = (EXCEL, SHEETS, NUMBERS, SQLITE)


def device_store(client: RapiClient, account: Account, context: BuildContext) -> Store:
    """Object-store databases for the SQLite backend, the CSV folder for the spreadsheet ones."""
    if account.backend == SQLITE.key:
        return device_table_store(client, account, context)
    return device_folder_store(client, account, context)


DEVICE_STORE = device_store
