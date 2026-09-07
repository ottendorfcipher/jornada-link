"""``jornada db``: look at the device's object-store databases (Pocket Outlook and others)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from .cedb import Record
from .pim.codecs import codec_for, generic
from .pim.store import DeviceStore, restore_snapshot
from .rapi import RapiClient

ACTIONS = ("ls", "dump", "snapshot", "restore")


def _fmt_time(stamp: Any) -> str:
    if not isinstance(stamp, (int, float)):
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def list_databases(client: RapiClient, out: Callable[[str], None] = print) -> int:
    databases = client.find_all_databases()
    out(f"{'records':>7}  {'bytes':>9}  {'type':>6}  {'modified':16}  name")
    for db in sorted(databases, key=lambda d: d.name.casefold()):
        out(f"{db.num_records:>7}  {db.size:>9,}  0x{db.db_type:04x}  {_fmt_time(db.last_modified):16}  {db.name}")
    out(f"{len(databases)} database(s)")
    return 0


def _record_view(record: Record, decode: Callable[[Record], Any], raw: bool) -> Dict[str, Any]:
    if raw:
        return record.to_json()
    decoded = decode(record)
    if isinstance(decoded, Record):
        return decoded.to_json()
    return {"oid": record.oid, **decoded.to_dict()}


def dump_database(client: RapiClient, name: str, as_json: bool, raw: bool, limit: int,
                  out: Callable[[str], None] = print) -> int:
    codec = codec_for(name) or generic(name)
    _info, records = client.read_all_records(codec.database)
    shown = records[:limit] if limit > 0 else records
    views: List[Dict[str, Any]] = []
    for record in shown:
        try:
            views.append(_record_view(record, codec.decode, raw))
        except (ValueError, TypeError, OverflowError) as exc:
            views.append({"oid": record.oid, "error": str(exc), **record.to_json()})
    if as_json:
        out(json.dumps(views, indent=1, ensure_ascii=False, default=str))
    else:
        for view in views:
            out(_format_view(view))
        out(f"{len(records)} record(s) in {codec.database!r}" + (f", showing {len(shown)}" if len(shown) < len(records) else ""))
    return 0


def _format_view(view: Dict[str, Any]) -> str:
    oid = view.get("oid", 0)
    if "props" in view:
        lines = [f"record 0x{oid:08x}"]
        for prop in view["props"]:
            lines.append(f"  0x{prop['id']:04x} {prop['kind']:8} {prop['value']!r}")
        return "\n".join(lines)
    fields = ", ".join(f"{k}={v!r}" for k, v in view.items() if k != "oid" and v not in ("", None, (), []))
    return f"record 0x{oid:08x}: {fields}"


def snapshot_database(client: RapiClient, name: str, directory: Path, out: Callable[[str], None] = print) -> int:
    codec = codec_for(name) or generic(name)
    store = DeviceStore(client, codec, snapshot_dir=directory, log=out)
    out(str(store.snapshot()))
    return 0


def run(args: argparse.Namespace, connect: Callable[[argparse.Namespace], RapiClient]) -> int:
    with connect(args) as client:
        if args.action == "ls":
            return list_databases(client)
        if args.action == "dump":
            if not args.name:
                raise SystemExit("usage: jornada db dump NAME [--json] [--raw] [--limit N]")
            return dump_database(client, args.name, args.json, args.raw, args.limit)
        if args.action == "snapshot":
            if not args.name:
                raise SystemExit("usage: jornada db snapshot NAME [--dir DIR]")
            from .pim.store import DEFAULT_SNAPSHOT_DIR
            return snapshot_database(client, args.name, Path(args.dir) if args.dir else DEFAULT_SNAPSHOT_DIR)
        if args.action == "restore":
            if not args.name:
                raise SystemExit("usage: jornada db restore SNAPSHOT.json")
            count = restore_snapshot(client, Path(args.name))
            return 0 if count >= 0 else 1
    sys.stderr.write(f"unknown db action {args.action}\n")
    return 2


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
               connect: Callable[[argparse.Namespace], RapiClient]) -> None:
    p = sub.add_parser("db", help="object-store databases: ls, dump NAME, snapshot NAME, restore FILE")
    p.add_argument("action", choices=ACTIONS)
    p.add_argument("name", nargs="?", help="database name (dump/snapshot) or snapshot file (restore)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--raw", action="store_true", help="show every property instead of the decoded record")
    p.add_argument("--limit", type=int, default=0, help="show at most N records")
    p.add_argument("--dir", help="where to write the snapshot")
    p.set_defaults(func=lambda args: run(args, connect))
