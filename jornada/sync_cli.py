"""``jornada sync``: accounts for modern services and the sync runs that use them."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .rapi import RapiClient
from .rapi_errors import RapiError
from .sync.accounts import (SECRET_KEYS, Account, AccountError, delete_secret, find_account, load_accounts,
                            read_secret, remove_account, save_accounts, state_path, sync_dir, update_secret,
                            upsert_account, validate_name)
from .sync.base import StoreError
from .sync.engine import Direction, Options, Prefer, apply, plan, refresh_hashes
from .sync.registry import BuildContext, ModuleSpec, load_modules, module_for
from .sync.state import load_state, save_state

DIRECTIONS = tuple(d.value for d in Direction)
PREFERENCES = tuple(p.value for p in Prefer)


def _sync_dir(args: argparse.Namespace) -> Path:
    return Path(args.sync_dir).expanduser() if getattr(args, "sync_dir", None) else sync_dir()


def _context(args: argparse.Namespace, account: Account, log: Callable[[str], None] = print) -> BuildContext:
    directory = _sync_dir(args)
    return BuildContext(
        log=log,
        sync_dir=directory,
        save_secrets=lambda changes: update_secret(account.name, changes, directory),
        device_ip=getattr(args, "ip", None),
    )


def _parse_pairs(pairs: Optional[list]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"expected key=value, got {pair!r}")
        values[key.strip()] = value
    return values


# -- account commands -----------------------------------------------------------
def cmd_modules(args: argparse.Namespace) -> int:
    for key, module in sorted(load_modules().items()):
        kind = "bridge" if module.is_bridge else "two-way store"
        print(f"{key}: {module.title} ({kind})")
        for setting in module.settings:
            print(f"    device setting {setting.key}: {setting.help}" + ("" if setting.required else " (optional)"))
        for backend in module.backends:
            print(f"  {backend.key}: {backend.title}")
            for setting in backend.settings:
                flags = ("secret" if setting.secret else "") + (", optional" if not setting.required else "")
                print(f"      {setting.key}: {setting.help}" + (f" [{flags.strip(', ')}]" if flags.strip(', ') else ""))
            if backend.notes:
                print(f"      note: {backend.notes}")
    return 0


def cmd_account_add(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    modules = load_modules()
    if args.module not in modules:
        raise SystemExit(f"unknown module {args.module!r}; known: {', '.join(sorted(modules)) or 'none'}")
    module = modules[args.module]
    backend = module.backend(args.backend)
    given = _parse_pairs(args.set)
    secret_keys = {s.key for s in backend.settings if s.secret} | set(SECRET_KEYS)
    for key in args.ask or []:
        given[key] = getpass.getpass(f"{key}: ")
    plain = {k: v for k, v in given.items() if k not in secret_keys}
    secrets = {k: v for k, v in given.items() if k in secret_keys}
    account = Account(name, module.key, backend.key, tuple(sorted(plain.items())))
    directory = _sync_dir(args)
    missing = backend.missing_settings(account, secrets)
    missing += tuple(s.key for s in module.settings if s.required and not account.setting(s.key))
    if missing and not backend.login:
        raise SystemExit(f"missing settings for {backend.title}: {', '.join(missing)} (use --set key=value or --ask key)")
    save_accounts(upsert_account(load_accounts(directory), account), directory)
    if secrets:
        update_secret(name, secrets, directory)
    print(f"saved account {name!r}: {module.title} via {backend.title}")
    if backend.login and (missing or args.login):
        return _login(account, backend.login, args)
    return 0


def _login(account: Account, login: Callable, args: argparse.Namespace) -> int:
    directory = _sync_dir(args)
    secrets = read_secret(account.name, directory)
    try:
        new_secrets = login(account, secrets, _context(args, account))
    except (AccountError, RuntimeError, OSError) as exc:
        sys.stderr.write(f"sign-in failed: {exc}\n")
        return 1
    if new_secrets:
        update_secret(account.name, new_secrets, directory)
    print(f"signed in for account {account.name!r}")
    return 0


def cmd_account_login(args: argparse.Namespace) -> int:
    account = find_account(load_accounts(_sync_dir(args)), args.name)
    backend = module_for(account).backend(account.backend)
    if backend.login is None:
        print(f"{backend.title} needs no sign-in step")
        return 0
    return _login(account, backend.login, args)


def cmd_account_list(args: argparse.Namespace) -> int:
    accounts = load_accounts(_sync_dir(args))
    if not accounts:
        print("no sync accounts yet — see `jornada sync modules` and `jornada sync account add`")
        return 0
    for account in sorted(accounts, key=lambda a: a.name):
        settings = ", ".join(f"{k}={v}" for k, v in account.settings)
        print(f"{account.name}: {account.module} via {account.backend}" + (f"  ({settings})" if settings else ""))
    return 0


def cmd_account_remove(args: argparse.Namespace) -> int:
    directory = _sync_dir(args)
    accounts = load_accounts(directory)
    find_account(accounts, args.name)
    save_accounts(remove_account(accounts, args.name), directory)
    delete_secret(args.name, directory)
    print(f"removed account {args.name!r} (its sync state file is kept)")
    return 0


# -- sync runs -------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> int:
    directory = _sync_dir(args)
    account = find_account(load_accounts(directory), args.name)
    state = load_state(state_path(account, directory))
    print(f"{account.name}: {account.module} via {account.backend}")
    print(f"  linked records: {len(state.links)}")
    print(f"  last sync: {state.last_sync or 'never'}")
    return 0


def run_sync(account: Account, module: ModuleSpec, client: Optional[RapiClient], secrets: Dict[str, Any],
             context: BuildContext, options: Options, dry_run: bool, state_file: Path,
             log: Callable[[str], None] = print) -> int:
    backend = module.backend(account.backend)
    missing = backend.missing_settings(account, secrets)
    if missing:
        raise AccountError(f"account {account.name!r} is missing {', '.join(missing)}; "
                           "run `jornada sync account login` or add the settings")
    if module.is_bridge:
        if dry_run:
            log(f"dry run: `jornada sync run {account.name}` would start the {backend.title} bridge")
            return 0
        return module.bridge(account, secrets, context)
    if client is None or module.device_store is None:
        raise AccountError(f"module {module.key!r} needs a device connection")
    local = module.device_store(client, account, context)
    remote = backend.build(account, secrets, context)
    state = load_state(state_file)
    local_items = local.list()
    remote_items = remote.list()
    log(f"device: {len(local_items)} record(s); {backend.title}: {len(remote_items)} record(s); "
        f"{len(state.links)} linked")
    plan_ = plan(local_items, remote_items, state, options)
    log(f"plan: {plan_.summary()}")
    if dry_run:
        for action in plan_.actions:
            if action.kind != "link":
                log("  " + action.describe())
        return 0
    new_state, result = apply(plan_, local, remote, state, log=lambda line: log("  " + line))
    save_state(state_file, new_state)   # links first: a failed re-read must not lose them
    if result.touched_local or result.touched_remote:
        try:
            new_state = refresh_hashes(new_state, local.list(), remote.list(),
                                       result.touched_local, result.touched_remote)
            save_state(state_file, new_state)
        except (StoreError, RapiError, OSError) as exc:
            log(f"warning: could not re-read the stores after writing ({exc}); "
                "the next run may report the records it just wrote as changed")
    log(f"done: {result.summary()}")
    for error in result.errors:
        log(f"  error: {error}")
    return 1 if result.errors else 0


def cmd_run(args: argparse.Namespace, connect: Callable[[argparse.Namespace], RapiClient]) -> int:
    directory = _sync_dir(args)
    account = find_account(load_accounts(directory), args.name)
    module = module_for(account)
    options = Options(Direction(args.direction), Prefer(args.prefer), not args.no_delete)
    secrets = read_secret(account.name, directory)
    context = _context(args, account)
    state_file = state_path(account, directory)
    try:
        if module.is_bridge:
            return run_sync(account, module, None, secrets, context, options, args.dry_run, state_file)
        with connect(args) as client:
            return run_sync(account, module, client, secrets, context, options, args.dry_run, state_file)
    except (AccountError, StoreError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
               connect: Callable[[argparse.Namespace], RapiClient]) -> None:
    p = sub.add_parser("sync", help="sync Pocket Outlook, notes and documents with modern apps and services")
    p.add_argument("--sync-dir", help=argparse.SUPPRESS)
    actions = p.add_subparsers(dest="sync_command", required=True)

    actions.add_parser("modules", help="list sync modules, backends and their settings").set_defaults(func=cmd_modules)

    account = actions.add_parser("account", help="add, list, login, remove sync accounts")
    account_actions = account.add_subparsers(dest="account_command", required=True)
    add = account_actions.add_parser("add", help="create or replace an account")
    add.add_argument("name")
    add.add_argument("--module", required=True, help="calendar, contacts, tasks, mail, notes, documents")
    add.add_argument("--backend", required=True, help="see `jornada sync modules`")
    add.add_argument("--set", action="append", metavar="KEY=VALUE", help="a setting (secrets go to the secret store)")
    add.add_argument("--ask", action="append", metavar="KEY", help="prompt for a secret setting without echo")
    add.add_argument("--login", action="store_true", help="run the backend's sign-in right away")
    add.set_defaults(func=cmd_account_add)
    for verb, handler in (("list", cmd_account_list), ("login", cmd_account_login), ("remove", cmd_account_remove)):
        sp = account_actions.add_parser(verb)
        if verb != "list":
            sp.add_argument("name")
        sp.set_defaults(func=handler)

    run = actions.add_parser("run", help="synchronize one account now")
    run.add_argument("name")
    run.add_argument("--dry-run", action="store_true", help="show the plan without changing anything")
    run.add_argument("--direction", choices=DIRECTIONS, default=Direction.BOTH.value)
    run.add_argument("--prefer", choices=PREFERENCES, default=Prefer.REMOTE.value,
                     help="which side wins when a record changed on both (default: remote)")
    run.add_argument("--no-delete", action="store_true", help="never propagate deletions")
    run.set_defaults(func=lambda args: cmd_run(args, connect))

    status = actions.add_parser("status", help="links and last sync time of an account")
    status.add_argument("name")
    status.set_defaults(func=cmd_status)
