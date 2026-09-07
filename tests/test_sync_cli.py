import json
from datetime import date
from pathlib import Path

import pytest

from jornada import cli
from jornada.pim import ids
from jornada.pim.models import Task
from jornada.sync import registry
from tests import fake_sync_module
from tests.fake_device import FakeRapiServer


@pytest.fixture
def device():
    server = FakeRapiServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def env(monkeypatch, tmp_path, device):
    monkeypatch.setattr(registry, "MODULE_PACKAGES", {"memtasks": "tests.fake_sync_module"})
    fake_sync_module.STORES.clear()
    fake_sync_module.LOGINS.clear()
    sync_dir = tmp_path / "sync"

    def run(*argv):
        return cli.main(["--ip", "127.0.0.1", "--rapi-port", str(device.port), "sync", "--sync-dir", str(sync_dir), *argv])

    run.sync_dir = sync_dir
    return run


def test_modules_and_account_lifecycle(env, capsys):
    assert env("modules") == 0
    out = capsys.readouterr().out
    assert "memtasks: Memory tasks" in out and "password: a secret [secret" in out
    assert env("account", "list") == 0 and "no sync accounts" in capsys.readouterr().out
    assert env("account", "add", "home", "--module", "memtasks", "--backend", "memory",
               "--set", "label=Home", "--set", "password=hunter2") == 0
    accounts = json.loads((env.sync_dir / "accounts.json").read_text())
    assert accounts["accounts"][0]["settings"] == {"label": "Home"}
    assert "hunter2" not in (env.sync_dir / "accounts.json").read_text()
    assert json.loads((env.sync_dir / "secrets" / "home.json").read_text()) == {"password": "hunter2"}
    assert env("account", "list") == 0 and "home: memtasks via memory  (label=Home)" in capsys.readouterr().out
    assert env("status", "home") == 0 and "last sync: never" in capsys.readouterr().out
    assert env("account", "remove", "home") == 0
    assert not (env.sync_dir / "secrets" / "home.json").exists()
    assert env("status", "home") == 1 and "no sync account" in capsys.readouterr().err


def test_account_add_validation(env, capsys):
    with pytest.raises(SystemExit):
        env("account", "add", "x", "--module", "nope", "--backend", "memory")
    with pytest.raises(SystemExit):
        env("account", "add", "x", "--module", "memtasks", "--backend", "memory")  # label missing
    with pytest.raises(SystemExit):
        env("account", "add", "x", "--module", "memtasks", "--backend", "memory", "--set", "label")
    assert env("account", "add", "bad name", "--module", "memtasks", "--backend", "memory", "--set", "label=x") == 1


def test_login_backend_runs_sign_in_and_stores_secret(env, capsys):
    assert env("account", "add", "cloud", "--module", "memtasks", "--backend", "oauthish") == 0
    assert fake_sync_module.LOGINS == ["cloud"]
    assert json.loads((env.sync_dir / "secrets" / "cloud.json").read_text()) == {"token": "fresh-token"}
    assert env("account", "login", "cloud") == 0 and fake_sync_module.LOGINS == ["cloud", "cloud"]
    assert env("account", "add", "plain", "--module", "memtasks", "--backend", "memory", "--set", "label=x") == 0
    assert env("account", "login", "plain") == 0 and "needs no sign-in" in capsys.readouterr().out


def test_run_syncs_both_ways_and_is_idempotent(env, device, capsys):
    assert env("account", "add", "home", "--module", "memtasks", "--backend", "memory", "--set", "label=x") == 0
    fake_sync_module.STORES["home"] = store = __import__("tests.memory_store", fromlist=["MemoryStore"]).MemoryStore(
        {"r1": Task("From the cloud", due=date(2026, 1, 2))})
    from jornada.cedb import PropVal
    db = device.db.create(ids.DB_TASKS)
    device.db.add_record(db, (PropVal.string(ids.SUBJECT, "From the device"),))
    assert env("run", "home", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "plan: 1 create local, 1 create remote" in out and "→ device" in out
    assert len(db.records) == 1 and len(store.records) == 1
    assert env("run", "home") == 0
    out = capsys.readouterr().out
    assert "done: 1 create remote, 1 create local" in out
    assert len(db.records) == 2 and {t.summary for t in store.records.values()} == {"From the cloud", "From the device"}
    assert list((env.sync_dir / "snapshots").glob("Tasks_Database.*.json"))
    assert env("run", "home") == 0 and "plan: nothing to do" in capsys.readouterr().out
    assert env("status", "home") == 0 and "linked records: 2" in capsys.readouterr().out
    assert env("run", "home", "--direction", "to-device", "--prefer", "local", "--no-delete") == 0


def test_run_reports_missing_settings_and_unknown_module(env, capsys, monkeypatch):
    assert env("account", "add", "cloud", "--module", "memtasks", "--backend", "oauthish") == 0
    (env.sync_dir / "secrets" / "cloud.json").write_text("{}")
    assert env("run", "cloud") == 1 and "missing token" in capsys.readouterr().err
    monkeypatch.setattr(registry, "MODULE_PACKAGES", {})
    assert env("run", "cloud") == 1 and "unknown module" in capsys.readouterr().err


def test_bridge_module_runs_without_a_device(monkeypatch, tmp_path, capsys):
    fake = lambda packages=None: {"membridge": fake_sync_module.BRIDGE_MODULE}
    monkeypatch.setattr(registry, "load_modules", fake)
    monkeypatch.setattr("jornada.sync_cli.load_modules", fake)
    sync_dir = str(tmp_path / "s")
    assert cli.main(["--ip", "10.0.0.5", "sync", "--sync-dir", sync_dir, "account", "add", "b", "--module", "membridge", "--backend", "memory"]) == 0
    assert cli.main(["--ip", "10.0.0.5", "sync", "--sync-dir", sync_dir, "run", "b"]) == 7
    assert fake_sync_module.BRIDGE_RUNS[-1] == ("b", "10.0.0.5")


def test_registry_skips_missing_packages_but_raises_on_broken_ones(tmp_path, monkeypatch):
    found = registry.load_modules({"x": "tests.no_such_module_here", "y": "tests.fake_sync_module"})
    assert list(found) == ["y"]
    broken = tmp_path / "broken_sync_module.py"
    broken.write_text("import definitely_missing_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError):
        registry.load_modules({"z": "broken_sync_module"})
    from jornada.sync.accounts import Account, AccountError
    with pytest.raises(AccountError):
        registry.module_for(Account("a", "nope", "x"), {})
    with pytest.raises(AccountError):
        fake_sync_module.MODULE.backend("nope")
    assert fake_sync_module.MODULE.backend("memory").missing_settings(Account("a", "memtasks", "memory"), {}) == ("label",)
