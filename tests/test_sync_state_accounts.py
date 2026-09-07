import os
import stat
from pathlib import Path

import pytest

from jornada.sync.accounts import (Account, AccountError, delete_secret, find_account, load_accounts, read_secret,
                                   remove_account, save_accounts, split_secrets, state_path, update_secret,
                                   upsert_account, validate_name, write_secret)
from jornada.sync.state import Link, SyncState, load_state, save_state


def test_state_round_trip_and_link_helpers(tmp_path: Path):
    state = SyncState().with_link(Link("1", "r1", "a", "b")).with_link(Link("2", "r2", "c", "d"))
    assert state.by_local()["2"].remote_id == "r2" and state.by_remote()["r1"].local_id == "1"
    replaced = state.with_link(Link("1", "r9", "x", "y"))
    assert {l.remote_id for l in replaced.links} == {"r9", "r2"}
    assert state.without(local_id="1").links == (Link("2", "r2", "c", "d"),)
    assert state.without(remote_id="r2").links == (Link("1", "r1", "a", "b"),)
    path = tmp_path / "state.json"
    save_state(path, state)
    assert load_state(path) == state
    assert load_state(tmp_path / "missing.json") == SyncState()


def test_accounts_round_trip_and_lookup(tmp_path: Path):
    account = Account("work", "calendar", "google", (("calendar_id", "primary"),))
    save_accounts((account,), tmp_path)
    loaded = load_accounts(tmp_path)
    assert loaded == (account,) and find_account(loaded, "work").setting("calendar_id") == "primary"
    assert account.setting("nope", "dflt") == "dflt"
    updated = account.with_setting("calendar_id", "team").with_setting("tz", "UTC")
    assert dict(updated.settings) == {"calendar_id": "team", "tz": "UTC"}
    saved = upsert_account(loaded, updated)
    assert len(saved) == 1 and saved[0].setting("tz") == "UTC"
    assert remove_account(saved, "work") == ()
    with pytest.raises(AccountError):
        find_account(loaded, "home")
    assert load_accounts(tmp_path / "empty") == ()


@pytest.mark.parametrize("bad", ["", "-x", "a b", "x" * 65, "we/ird"])
def test_account_name_validation(bad):
    with pytest.raises(AccountError):
        validate_name(bad)
    assert validate_name("ok-name_1.x") == "ok-name_1.x"


def test_secrets_are_private_files(tmp_path: Path):
    written = write_secret("acct", {"token": "t"}, tmp_path)
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(written.parent).st_mode) == 0o700
    assert read_secret("acct", tmp_path) == {"token": "t"}
    assert update_secret("acct", {"refresh": "r"}, tmp_path) == {"token": "t", "refresh": "r"}
    assert read_secret("nobody", tmp_path) == {}
    delete_secret("acct", tmp_path)
    assert read_secret("acct", tmp_path) == {}
    delete_secret("acct", tmp_path)
    (tmp_path / "secrets" / "broken.json").write_text("{not json")
    with pytest.raises(AccountError):
        read_secret("broken", tmp_path)


def test_split_secrets_and_state_path(tmp_path: Path):
    plain, secret = split_secrets({"user": "u", "password": "p", "token": "t"})
    assert plain == {"user": "u"} and secret == {"password": "p", "token": "t"}
    account = Account("home", "tasks", "apple")
    assert state_path(account, tmp_path) == tmp_path / "state" / "tasks-home.json"
