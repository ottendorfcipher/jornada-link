"""Where the sync modules (calendar, contacts, tasks, mail, notes, documents) and their
backends describe themselves to the CLI and the app.

A module package exposes ``MODULE: ModuleSpec``. Store-type modules provide a
device store factory and one backend per modern service; the mail module is a
bridge (it runs a local server for the device's own Inbox) and provides
``bridge`` instead of a device store.
"""
from __future__ import annotations

import webbrowser
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ..rapi import RapiClient
from ..webapi.applescript import Runner
from ..webapi.http import Transport
from .accounts import Account, AccountError
from .base import Store


@dataclass(frozen=True)
class SettingSpec:
    key: str
    help: str
    required: bool = True
    secret: bool = False
    default: Optional[str] = None


@dataclass(frozen=True)
class BuildContext:
    """What a backend may use besides its account: logging, secret persistence, test hooks."""

    log: Callable[[str], None]
    sync_dir: Path
    save_secrets: Callable[[Dict[str, Any]], None]
    http_transport: Optional[Transport] = None
    runner: Optional[Runner] = None
    open_browser: Callable[[str], Any] = webbrowser.open
    device_ip: Optional[str] = None
    listen_ip: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


Builder = Callable[[Account, Dict[str, Any], BuildContext], Store]
Login = Callable[[Account, Dict[str, Any], BuildContext], Dict[str, Any]]
Bridge = Callable[[Account, Dict[str, Any], BuildContext], int]
DeviceFactory = Callable[[RapiClient, Account, BuildContext], Store]


@dataclass(frozen=True)
class BackendSpec:
    key: str
    title: str
    settings: Tuple[SettingSpec, ...]
    build: Builder
    login: Optional[Login] = None
    notes: str = ""

    def missing_settings(self, account: Account, secrets: Dict[str, Any]) -> Tuple[str, ...]:
        missing = []
        for spec in self.settings:
            if not spec.required:
                continue
            present = secrets.get(spec.key) if spec.secret else account.setting(spec.key)
            if not present:
                missing.append(spec.key)
        return tuple(missing)


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    title: str
    backends: Tuple[BackendSpec, ...]
    device_store: Optional[DeviceFactory] = None
    bridge: Optional[Bridge] = None
    settings: Tuple[SettingSpec, ...] = ()
    notes: str = ""

    def backend(self, key: str) -> BackendSpec:
        for spec in self.backends:
            if spec.key == key:
                return spec
        known = ", ".join(b.key for b in self.backends)
        raise AccountError(f"module {self.key!r} has no backend {key!r} (known: {known})")

    @property
    def is_bridge(self) -> bool:
        return self.bridge is not None


MODULE_PACKAGES = {
    "calendar": "jornada.sync.calendar",
    "contacts": "jornada.sync.contacts",
    "tasks": "jornada.sync.tasks",
    "mail": "jornada.sync.mail",
    "notes": "jornada.sync.notes",
    "documents": "jornada.sync.documents",
}


def load_modules(packages: Optional[Dict[str, str]] = None) -> Dict[str, ModuleSpec]:
    """Import every module package that exists; a package that is missing is skipped,
    a package that fails to import raises (that is a bug, not an absence)."""
    found: Dict[str, ModuleSpec] = {}
    for key, package in (packages or MODULE_PACKAGES).items():
        try:
            module = import_module(package)
        except ModuleNotFoundError as exc:
            if exc.name == package or (exc.name and package.startswith(exc.name + ".")):
                continue
            raise
        spec = getattr(module, "MODULE", None)
        if isinstance(spec, ModuleSpec):
            found[key] = spec
    return found


def module_for(account: Account, modules: Optional[Dict[str, ModuleSpec]] = None) -> ModuleSpec:
    catalogue = modules if modules is not None else load_modules()
    try:
        return catalogue[account.module]
    except KeyError:
        known = ", ".join(sorted(catalogue)) or "none"
        raise AccountError(f"account {account.name!r} uses unknown module {account.module!r} (known: {known})") from None
