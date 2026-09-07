"""Documents module: Pocket Word / Pocket Excel / Pocket Access ⇄ Word, Google Docs, Pages,
Excel, Google Sheets, Numbers, SQLite.

Two families of backends live here — text documents (``text_backends``) and
spreadsheets / tables (``sheet_backends``). Each family registers its backends
and the device store it needs; this package only assembles the module spec.
"""
from __future__ import annotations

from typing import Tuple

from ...rapi import RapiClient
from ..accounts import Account
from ..base import Store
from ..registry import BackendSpec, BuildContext, ModuleSpec, SettingSpec

DEFAULT_FOLDER = "\\My Documents"


def _collect() -> Tuple[Tuple[BackendSpec, ...], dict]:
    backends: Tuple[BackendSpec, ...] = ()
    device_factories = {}
    for name in ("text_backends", "sheet_backends"):
        try:
            module = __import__(f"{__name__}.{name}", fromlist=["BACKENDS", "DEVICE_STORE"])
        except ModuleNotFoundError as exc:
            if exc.name == f"{__name__}.{name}":
                continue
            raise
        backends += tuple(module.BACKENDS)
        for backend in module.BACKENDS:
            device_factories[backend.key] = module.DEVICE_STORE
    return backends, device_factories


_BACKENDS, _DEVICE_FACTORIES = _collect()


def device_store(client: RapiClient, account: Account, context: BuildContext) -> Store:
    factory = _DEVICE_FACTORIES.get(account.backend)
    if factory is None:
        raise ValueError(f"documents backend {account.backend!r} has no device store")
    return factory(client, account, context)


MODULE = ModuleSpec(
    key="documents",
    title="Documents and spreadsheets",
    backends=_BACKENDS,
    device_store=device_store,
    settings=(SettingSpec("folder", f"device folder to sync (default {DEFAULT_FOLDER})", required=False,
                          default=DEFAULT_FOLDER),),
    notes="Pocket Word opens .txt and .rtf, Pocket Excel opens tab/comma-separated text; binary .pwd/.pxl are not converted.",
)
