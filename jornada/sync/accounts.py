"""Sync accounts (which backend, which settings) and their secrets.

Settings live in ``~/.jornada-link/sync/accounts.json``; anything sensitive
(passwords, OAuth tokens) lives one file per account under ``secrets/`` with
mode 0600 and is never logged.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..state import read_state, write_state

DEFAULT_SYNC_DIR = Path.home() / ".jornada-link" / "sync"
ENV_SYNC_DIR = "JORNADA_SYNC_DIR"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SECRET_KEYS = ("password", "token", "secret", "refresh_token", "access_token", "client_secret")


class AccountError(ValueError):
    """Bad account name, unknown account, or unusable settings."""


def sync_dir() -> Path:
    override = os.environ.get(ENV_SYNC_DIR, "").strip()
    return Path(override).expanduser() if override else DEFAULT_SYNC_DIR


def validate_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise AccountError(f"account name {name!r} must be 1-64 letters, digits, '.', '_' or '-'")
    return name


@dataclass(frozen=True)
class Account:
    name: str
    module: str
    backend: str
    settings: Tuple[Tuple[str, str], ...] = ()

    def setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        for k, v in self.settings:
            if k == key:
                return v
        return default

    def with_setting(self, key: str, value: str) -> "Account":
        kept = tuple((k, v) for k, v in self.settings if k != key)
        return replace(self, settings=kept + ((key, value),))

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "module": self.module, "backend": self.backend,
                "settings": {k: v for k, v in self.settings}}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Account":
        settings = data.get("settings") or {}
        return cls(validate_name(str(data["name"])), str(data["module"]), str(data["backend"]),
                   tuple((str(k), str(v)) for k, v in settings.items()))


def accounts_path(directory: Optional[Path] = None) -> Path:
    return (directory or sync_dir()) / "accounts.json"


def load_accounts(directory: Optional[Path] = None) -> Tuple[Account, ...]:
    data = read_state(accounts_path(directory))
    if not data:
        return ()
    return tuple(Account.from_dict(entry) for entry in data.get("accounts", []))


def save_accounts(accounts: Tuple[Account, ...], directory: Optional[Path] = None) -> None:
    write_state(accounts_path(directory), {"accounts": [a.to_dict() for a in accounts]})


def find_account(accounts: Tuple[Account, ...], name: str) -> Account:
    for account in accounts:
        if account.name == name:
            return account
    raise AccountError(f"no sync account called {name!r} (see `jornada sync account list`)")


def upsert_account(accounts: Tuple[Account, ...], account: Account) -> Tuple[Account, ...]:
    return tuple(a for a in accounts if a.name != account.name) + (account,)


def remove_account(accounts: Tuple[Account, ...], name: str) -> Tuple[Account, ...]:
    return tuple(a for a in accounts if a.name != name)


def split_secrets(settings: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Separate sensitive keys from plain settings so they never land in accounts.json."""
    plain = {k: v for k, v in settings.items() if k not in SECRET_KEYS}
    secret = {k: v for k, v in settings.items() if k in SECRET_KEYS}
    return plain, secret


# -- secrets -------------------------------------------------------------------
def secret_path(name: str, directory: Optional[Path] = None) -> Path:
    return (directory or sync_dir()) / "secrets" / f"{validate_name(name)}.json"


def read_secret(name: str, directory: Optional[Path] = None) -> Dict[str, Any]:
    try:
        with open(secret_path(name, directory), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise AccountError(f"cannot read the secrets of account {name!r}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def write_secret(name: str, data: Dict[str, Any], directory: Optional[Path] = None) -> Path:
    target = secret_path(name, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".secret-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return target


def update_secret(name: str, changes: Dict[str, Any], directory: Optional[Path] = None) -> Dict[str, Any]:
    merged = {**read_secret(name, directory), **changes}
    write_secret(name, merged, directory)
    return merged


def delete_secret(name: str, directory: Optional[Path] = None) -> None:
    try:
        os.unlink(secret_path(name, directory))
    except FileNotFoundError:
        pass


def state_path(account: Account, directory: Optional[Path] = None) -> Path:
    return (directory or sync_dir()) / "state" / f"{account.module}-{account.name}.json"
