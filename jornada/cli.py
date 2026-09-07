"""Command-line interface: ``jornada <subcommand> ...``."""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .constants import DEFAULT_REMOTE_IP, RAPI_PORT
from .dccm import DccmServer
from .rapi import RapiClient, RapiError
from .state import DEFAULT_STATE_PATH, read_state
from .transport import TransportError


def _device_ip(args: argparse.Namespace) -> str:
    if args.ip:
        return args.ip
    state = read_state(DEFAULT_STATE_PATH)
    if state and isinstance(state.get("ip"), str):
        return state["ip"]
    return DEFAULT_REMOTE_IP


def _ppp_interface_present() -> bool:
    try:
        return any(name.startswith("ppp") for _idx, name in socket.if_nameindex())
    except OSError:
        return True  # cannot enumerate: fall through to the real connection attempt


def _connect(args: argparse.Namespace) -> RapiClient:
    ip = _device_ip(args)
    if ip.startswith("192.168.131.") and not _ppp_interface_present():
        raise SystemExit(
            "the PPP link is down (no ppp interface exists).\n"
            "Run `sudo bin/jornada-ppp`, then start PC Link on the Jornada, and retry."
        )
    state = read_state(DEFAULT_STATE_PATH) or {}
    client = RapiClient(ip, args.rapi_port)
    try:
        client.connect(password=args.password, key=int(state.get("key", 0)))
    except OSError as exc:
        raise SystemExit(
            f"cannot reach the Jornada's RAPI port at {ip}:{args.rapi_port} ({exc}).\n"
            "Is the PPP link up (sudo bin/jornada-ppp), is `jornada dccm` running, "
            "and did you start PC Link on the device?"
        ) from exc
    return client


def _fmt_size(size: int) -> str:
    return f"{size:,}"


def _fmt_time(ts: Optional[float]) -> str:
    if ts is None:
        return "                "
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def cmd_dccm(args: argparse.Namespace) -> int:
    server = DccmServer(password=args.password, port=args.port)
    try:
        server.open()
    except OSError as exc:
        raise SystemExit(f"cannot listen on port {args.port}: {exc} (is another `jornada dccm` running?)") from exc
    logging.getLogger("jornada").info("waiting for the Jornada (start PC Link on the device)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger("jornada").info("stopping")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = read_state(DEFAULT_STATE_PATH)
    if state:
        dev = state.get("device") or {}
        print(f"dccm session: {dev.get('name', '?')} ({dev.get('device_class', '?')}, "
              f"{dev.get('hardware', '?')}) at {state.get('ip')}")
    else:
        print("dccm session: none recorded (is `jornada dccm` running and PC Link connected?)")
    with _connect(args) as client:
        version = client.get_version()
        print(f"Windows CE {version.major}.{version.minor:02d} build {version.build} {version.csd_version}".rstrip())
        store = client.get_store_information()
        print(f"object store: {_fmt_size(store.free_size)} free of {_fmt_size(store.store_size)} bytes")
        power = client.get_power_status()
        ac = {0: "battery", 1: "AC power"}.get(power.ac_line_status, f"ac={power.ac_line_status}")
        pct = "?" if power.battery_percent == 255 else f"{power.battery_percent}%"
        print(f"power: {ac}, main battery {pct}, backup flag 0x{power.backup_battery_flag:02x}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    with _connect(args) as client:
        entries = client.listdir(args.path)
    for entry in sorted(entries, key=lambda e: (not e.is_dir, e.name.lower())):
        kind = "<DIR>" if entry.is_dir else _fmt_size(entry.size).rjust(12)
        print(f"{_fmt_time(entry.mtime)}  {kind:>12}  {entry.name}")
    print(f"{len(entries)} item(s) in {args.path}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    local = Path(args.local) if args.local else Path(args.remote.rsplit("\\", 1)[-1])
    started = time.monotonic()
    total = 0
    with _connect(args) as client, open(local, "wb") as out:
        for chunk in client.iter_download(args.remote):
            out.write(chunk)
            total += len(chunk)
            sys.stderr.write(f"\r{_fmt_size(total)} bytes")
            sys.stderr.flush()
    elapsed = max(time.monotonic() - started, 1e-6)
    sys.stderr.write(f"\rdownloaded {args.remote} -> {local} ({_fmt_size(total)} bytes, {total / elapsed / 1024:.1f} KB/s)\n")
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    local = Path(args.local)
    if not local.is_file():
        raise SystemExit(f"{local} is not a file")
    remote = args.remote or f"\\My Documents\\{local.name}"
    if remote.endswith("\\"):
        remote += local.name
    data = local.read_bytes()
    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        sys.stderr.write(f"\r{_fmt_size(done)}/{_fmt_size(total)} bytes")
        sys.stderr.flush()

    with _connect(args) as client:
        written = client.upload(remote, data, progress=progress)
    elapsed = max(time.monotonic() - started, 1e-6)
    sys.stderr.write(f"\ruploaded {local} -> {remote} ({_fmt_size(written)} bytes, {written / elapsed / 1024:.1f} KB/s)\n")
    _mirror_after_send(args, data, remote, source=str(local))
    return 0


def _mirror_after_send(args: argparse.Namespace, data: bytes, remote: str, source: str) -> None:
    """Archive a successfully-sent payload on the Mac (see jornada.sendmirror)."""
    if getattr(args, "no_mirror", False):
        return
    from .sendmirror import mirror_sent
    try:
        mirrored = mirror_sent(data, remote, source=source)
        sys.stderr.write(f"mirrored to {mirrored}\n")
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"warning: sent OK, but could not mirror locally: {exc}\n")


def cmd_rm(args: argparse.Namespace) -> int:
    with _connect(args) as client:
        if args.recursive:
            from .remove import remove_tree
            try:
                stats = remove_tree(client, args.remote)
            except ValueError as exc:
                sys.stderr.write(f"error: {exc}\n")
                return 1
            print(f"removed {stats.files_deleted} file(s), {stats.directories_deleted} "
                  f"director(ies), {len(stats.errors)} error(s)")
            for error in stats.errors:
                print(f"  !! {error}")
            return 1 if stats.errors else 0
        client.delete_file(args.remote)
    print(f"deleted {args.remote}")
    return 0


def cmd_mkdir(args: argparse.Namespace) -> int:
    with _connect(args) as client:
        client.create_directory(args.remote)
    print(f"created {args.remote}")
    return 0


def cmd_rmdir(args: argparse.Namespace) -> int:
    with _connect(args) as client:
        client.remove_directory(args.remote)
    print(f"removed {args.remote}")
    return 0


def cmd_mv(args: argparse.Namespace) -> int:
    with _connect(args) as client:
        client.move_file(args.src, args.dst)
    print(f"moved {args.src} -> {args.dst}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    with _connect(args) as client:
        pid = client.create_process(args.exe, " ".join(args.args) if args.args else None)
    print(f"started {args.exe} (pid {pid})")
    return 0


def cmd_shortcut(args: argparse.Namespace) -> int:
    with _connect(args) as client:
        client.create_shortcut(args.shortcut, args.target)
    print(f"created {args.shortcut} -> {args.target}")
    return 0


def cmd_settime(args: argparse.Namespace) -> int:
    with _connect(args) as client:
        client.sync_time_from_mac()
    print(f"device clock set from this Mac ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from .backup import backup_tree
    destination = Path(args.destination).expanduser()
    with _connect(args) as client:
        print(f"backing up {args.path} -> {destination}")
        stats = backup_tree(client, args.path, destination,
                            include_rom=args.include_rom, include_cards=args.include_cards)
    print(f"done: {stats.files} file(s), {_fmt_size(stats.bytes_copied)} bytes copied, "
          f"{stats.skipped_existing} unchanged, {stats.skipped_rom} ROM skipped, "
          f"{len(stats.errors)} error(s)")
    for error in stats.errors:
        print(f"  !! {error}")
    return 1 if stats.errors else 0


def cmd_restore(args: argparse.Namespace) -> int:
    from .restore import free_space_warning, plan_size, restore_tree
    source = Path(args.source).expanduser()
    if not source.is_dir():
        raise SystemExit(f"{source} is not a directory")
    with _connect(args) as client:
        planned = plan_size(source)
        if not args.dry_run:
            warning = free_space_warning(client, planned)
            if warning:
                print(f"warning: {warning}")
        print(f"{'planning' if args.dry_run else 'restoring'} {source} -> {args.path} "
              f"(up to {_fmt_size(planned)} bytes before skips)")
        stats = restore_tree(client, source, args.path,
                             force=args.force, dry_run=args.dry_run)
    for error in stats.errors:
        print(f"  !! {error}")
    return 1 if stats.errors else 0


def cmd_install(args: argparse.Namespace) -> int:
    local = Path(args.cab)
    if not local.is_file():
        raise SystemExit(f"{local} is not a file")
    remote = f"\\Temp\\{local.name}"
    data = local.read_bytes()
    with _connect(args) as client:
        print(f"copying {local.name} ({_fmt_size(len(data))} bytes) to {remote}")
        client.upload(remote, data)
        pid = client.create_process("\\Windows\\wceload.exe", remote)
    print(f"installer launched on the device (pid {pid}) — follow the prompts on the Jornada")
    _mirror_after_send(args, data, remote, source=str(local))
    return 0


def cmd_ppplog(args: argparse.Namespace) -> int:
    from .ppprecord import dump
    path = args.record or str(Path.home() / ".jornada-link" / "ppp.record")
    try:
        for line in dump(path):
            print(line)
    except FileNotFoundError:
        raise SystemExit(f"{path} not found — bring the link up first (sudo bin/jornada-ppp records there)")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .serial_probe import sniff
    sniff(args.device, args.baud, args.seconds)
    return 0


def cmd_gpib(args: argparse.Namespace) -> int:
    from .gpib import GATEWAY_PORT, GatewayAddress, GpibError, GpibGateway

    address = GatewayAddress(_device_ip(args), args.gateway_port or GATEWAY_PORT)
    pad = args.address
    try:
        with GpibGateway(address, timeout=args.timeout) as gw:
            action = args.action
            if action == "ver":
                print(gw.version())
            elif action == "idn":
                print(gw.query(pad, "*IDN?"))
            elif action == "query":
                print(gw.query(pad, " ".join(args.text)))
            elif action == "write":
                gw.write(pad, " ".join(args.text))
            elif action == "read":
                print(gw.read(pad))
            elif action == "spoll":
                stb = gw.serial_poll(pad)
                print(f"status byte 0x{stb:02x} ({stb}){' requesting service' if stb & 0x40 else ''}")
            elif action == "ifc":
                gw.interface_clear()
            elif action == "clear":
                gw.device_clear(pad)
            elif action == "trigger":
                gw.trigger(pad)
            elif action == "local":
                gw.local(pad)
            elif action == "quit":
                gw.quit_gateway()
            elif action == "repl":
                _gpib_repl(gw, pad)
            else:
                raise SystemExit(f"unknown gpib action {action}")
    except GpibError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    return 0


_GPIB_QUERY_COMMANDS = {"ver", "spoll", "srq", "lines", "help", "read"}
_GPIB_SETTINGS = {"addr", "auto", "eoi", "eos", "read_tmo_ms", "mode"}


def _gpib_replies(line: str) -> bool:
    """True when a ++ command produces an answer (queries, or settings asked without a value)."""
    words = line[2:].split()
    if not words:
        return False
    return words[0] in _GPIB_QUERY_COMMANDS or (words[0] in _GPIB_SETTINGS and len(words) == 1)


def _gpib_repl(gw, pad: int) -> None:
    print(f"GPIB address {pad}; lines ending in ? are queried, others written; ++ lines go to the gateway; ^D ends")
    while True:
        try:
            line = input("gpib> ").strip()
        except EOFError:
            print()
            return
        if not line:
            continue
        if line.startswith("++"):
            gw.send_line(line)
            if _gpib_replies(line):
                try:
                    print(gw.read_line())
                except GpibError as exc:
                    print(f"({exc})")
        elif line.endswith("?"):
            try:
                print(gw.query(pad, line))
            except GpibError as exc:
                print(f"({exc})")
        else:
            gw.write(pad, line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jornada", description="Talk to an HP Jornada / Windows CE 2.x over serial PPP")
    parser.add_argument("--version", action="version", version=f"jornada-link {__version__}")
    parser.add_argument("--ip", help="device IP (default: from the dccm session, else 192.168.131.201)")
    parser.add_argument("--password", default=os.environ.get("JORNADA_PASSWORD") or None,
                        help="device password, if one is set on the Jornada (or set JORNADA_PASSWORD)")
    parser.add_argument("--rapi-port", type=int, default=RAPI_PORT, help=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dccm", help="listen on port 5679 for the device (run before PC Link)")
    p.add_argument("--port", type=int, default=5679)
    p.set_defaults(func=cmd_dccm)

    sub.add_parser("status", help="OS version, storage, battery").set_defaults(func=cmd_status)

    p = sub.add_parser("ls", help="list a device directory")
    p.add_argument("path", nargs="?", default="\\")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("get", help="copy a file device -> Mac")
    p.add_argument("remote")
    p.add_argument("local", nargs="?")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("put", help="copy a file Mac -> device")
    p.add_argument("local")
    p.add_argument("remote", nargs="?", help=r"default: \My Documents\<name>")
    p.add_argument("--no-mirror", action="store_true",
                   help="skip the Mac-side sent-file archive")
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("rm", help="delete a device file (or a whole subtree with -r)")
    p.add_argument("remote")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="recursively delete a directory and all its contents")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("mkdir", help="create a device directory")
    p.add_argument("remote")
    p.set_defaults(func=cmd_mkdir)

    p = sub.add_parser("rmdir", help="remove an empty device directory")
    p.add_argument("remote")
    p.set_defaults(func=cmd_rmdir)

    p = sub.add_parser("mv", help="rename/move on the device")
    p.add_argument("src")
    p.add_argument("dst")
    p.set_defaults(func=cmd_mv)

    p = sub.add_parser("run", help="launch a program on the device")
    p.add_argument("exe")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("shortcut", help="create a .lnk on the device")
    p.add_argument("shortcut", help=r"e.g. '\Windows\Desktop\Frotz.lnk'")
    p.add_argument("target", help=r"e.g. '\Program Files\Frotz\frotz.exe'")
    p.set_defaults(func=cmd_shortcut)

    sub.add_parser("settime", help="set the Jornada's clock from this Mac").set_defaults(func=cmd_settime)

    p = sub.add_parser("backup", help="recursively copy a device subtree to the Mac")
    p.add_argument("destination", help="local directory for the backup")
    p.add_argument("path", nargs="?", default="\\", help="device root to back up (default: whole store)")
    p.add_argument("--include-rom", action="store_true", help="also copy ROM-resident files")
    p.add_argument("--include-cards", action="store_true", help="also copy Storage Card contents")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="push a backup or sent-mirror tree back onto the device")
    p.add_argument("source", help="local tree made by `backup` or the sent-file archive")
    p.add_argument("path", nargs="?", default="\\", help="device destination root (default: \\)")
    p.add_argument("--force", action="store_true", help="resend even when name+size already match")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent without writing anything")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("install", help="copy a .cab to the device and launch its installer")
    p.add_argument("cab", help="CAB file built for Windows CE 2.x SH3")
    p.add_argument("--no-mirror", action="store_true",
                   help="skip the Mac-side sent-file archive")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("ppplog", help="decode the pppd record file into readable PPP frames")
    p.add_argument("record", nargs="?", help="default: ~/.jornada-link/ppp.record")
    p.set_defaults(func=cmd_ppplog)

    p = sub.add_parser("gpib", help="talk to an instrument through the jornada-gpib gateway on the device")
    p.add_argument("action", choices=["ver", "idn", "query", "write", "read", "spoll", "ifc", "clear", "trigger", "local", "repl", "quit"])
    p.add_argument("text", nargs="*", help="instrument command for query/write")
    p.add_argument("-a", "--address", type=int, default=1, help="GPIB primary address (default 1)")
    p.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for an answer")
    p.add_argument("--gateway-port", type=int, default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_gpib)

    from .usb_cli import add_parser as add_usb_parser
    add_usb_parser(sub)

    p = sub.add_parser("probe", help="sniff the serial line (no root needed)")
    p.add_argument("device")
    p.add_argument("baud", nargs="?", type=int, default=19200)
    p.add_argument("seconds", nargs="?", type=float, default=8.0)
    p.set_defaults(func=cmd_probe)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return args.func(args)
    except (RapiError, TransportError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
