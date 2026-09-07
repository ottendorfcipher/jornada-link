"""A minimal sync module for CLI tests: tasks on the device ⇄ an in-memory store."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict

from jornada.pim.codecs import TASKS
from jornada.pim.store import DeviceStore
from jornada.rapi import RapiClient
from jornada.sync.accounts import Account
from jornada.sync.registry import BackendSpec, BuildContext, ModuleSpec, SettingSpec
from tests.memory_store import MemoryStore

STORES: Dict[str, MemoryStore] = {}
LOGINS: list = []


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> MemoryStore:
    return STORES.setdefault(account.name, MemoryStore())


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    LOGINS.append(account.name)
    return {"token": "fresh-token"}


def device(client: RapiClient, account: Account, context: BuildContext) -> DeviceStore:
    return DeviceStore(client, replace(TASKS, create_if_missing=True),
                       snapshot_dir=context.sync_dir / "snapshots", log=context.log)


MODULE = ModuleSpec(
    key="memtasks",
    title="Memory tasks",
    backends=(
        BackendSpec("memory", "In-memory store",
                    (SettingSpec("label", "a label"), SettingSpec("password", "a secret", required=False, secret=True)),
                    build),
        BackendSpec("oauthish", "Needs sign-in", (SettingSpec("token", "token", secret=True),), build, login=login),
    ),
    device_store=device,
)

BRIDGE_RUNS: list = []


def bridge(account: Account, secrets: Dict[str, Any], context: BuildContext) -> int:
    BRIDGE_RUNS.append((account.name, context.device_ip))
    return 7


BRIDGE_MODULE = ModuleSpec(key="membridge", title="Memory bridge",
                           backends=(BackendSpec("memory", "In-memory", (), build),), bridge=bridge)
