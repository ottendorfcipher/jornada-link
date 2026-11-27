# jornada-link

[![CI](https://github.com/ottendorfcipher/jornada-link/actions/workflows/ci.yml/badge.svg)](https://github.com/ottendorfcipher/jornada-link/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![macOS 15+](https://img.shields.io/badge/macOS-15%2B-black.svg)](https://www.apple.com/macos/)

Talk to a **vintage HP Jornada / Windows CE 2.x handheld** from a modern Mac
over a USB-serial (FTDI) cable — list files, copy in both directions, make
folders, launch programs, set the clock, back up the whole device — with
**nothing installed on the handheld**. It speaks the device's own
ActiveSync-era protocols directly: PPP over serial, the "dccm" connection
notifier on TCP 5679, and the RAPI remote API on TCP 990.

Two front ends, one protocol core:

- **`jornada`** — a dependency-free Python CLI.
- **Jornada Sync.app** — a native SwiftUI macOS app styled after Microsoft
  ActiveSync (drag-and-drop file browser, device banner, transfers, logs).

```
HP Jornada ──serial sync cable──► FTDI FT232R ──USB──► Mac
   192.168.131.201  ◄──────────── PPP ────────────►  192.168.131.102
```

Developed and tested against an **HP Jornada 680e** (Windows CE 2.11 /
H/PC Pro 3.0, Hitachi SH3). Other CE 2.x H/PC devices use the same protocols and
should work; reports welcome.

> **Note** — this is an independent interoperability tool, not affiliated with HP
> or Microsoft. It talks to hardware you already own. See [`NOTICE.md`](NOTICE.md).

## Requirements

- A Mac (macOS 15+) with a USB-serial adapter (FTDI needs no driver; Prolific,
  Silicon Labs and WCH chips are recognised too) and the HP serial sync cable
  or the dock's DB-9 port.
- Python 3.9+ for the CLI (uses only the standard library).
- Xcode 16 / Swift 6 toolchain to build the app (optional).
- Administrator rights are needed **only** to start `pppd` (the PPP link).

## Quick start (CLI)

```bash
git clone https://github.com/ottendorfcipher/jornada-link.git
cd jornada-link
```

1. **Bring up the serial PPP link** (needs `sudo`, since `pppd` creates the
   network interface; this also starts the connection listener):

   ```bash
   sudo ./bin/jornada-ppp
   ```

   Serial port selection order: argument › `$JORNADA_SERIAL` ›
   `~/.jornada-link/serial` › first `/dev/cu.usbserial-*`. Baud selection:
   argument › `$JORNADA_BAUD` › `~/.jornada-link/baud` › `115200`. Explicit form:

   ```bash
   sudo ./bin/jornada-ppp /dev/cu.usbserial-XXXX 115200
   ```

2. **On the Jornada:** Start ▸ Programs ▸ Communication ▸ **PC Link** (set the
   matching rate under Control Panel ▸ Communications ▸ PC Connection). You'll
   see the `CLIENT`/`CLIENTSERVER` handshake, PPP negotiate, then the link
   reports the device.

3. **Use it** (a second terminal, no `sudo` needed):

   ```bash
   ./bin/jornada status                       # OS version, storage, battery
   ./bin/jornada ls '\'                        # root of the object store
   ./bin/jornada ls '\My Documents'
   ./bin/jornada get '\My Documents\notes.pwd' ./notes.pwd
   ./bin/jornada put ./photo.bmp '\My Documents\'
   ./bin/jornada mkdir '\My Documents\Mac'
   ./bin/jornada mv '\My Documents\a.txt' '\My Documents\Mac\a.txt'
   ./bin/jornada rm '\My Documents\old.txt'
   ./bin/jornada run '\Windows\pword.exe' '\My Documents\notes.pwd'
   ./bin/jornada settime                       # set the device clock from the Mac
   ./bin/jornada backup ./jornada-backup       # recursively copy the object store
   ./bin/jornada install ./app-sh3.cab         # copy a CAB and launch its installer
   ```

   Device paths use backslashes — single-quote them in the shell. Transfers run
   at roughly 2.5–9 KB/s over a 115200 line, so be patient with large files.

### Command reference

| Command | Does |
|---|---|
| `status` | OS version, object-store usage, battery/power |
| `ls PATH` | List a device directory |
| `get REMOTE [LOCAL]` | Copy a file device → Mac |
| `put LOCAL [REMOTE]` | Copy a file Mac → device |
| `rm [-r]` / `mkdir` / `rmdir` / `mv` | File management (`rm -r` deletes a whole subtree) |
| `run EXE [ARGS…]` | Launch a program on the device |
| `settime [--utc]` | Set the Jornada's date and time from this Mac (local wall-clock; `--utc` if the device's zone is set) |
| `shortcut LNK TARGET` | Create a `.lnk` on the device |
| `backup DEST [PATH]` | Recursively mirror a device subtree + JSON manifest |
| `restore SOURCE [PATH]` | Push a backup or sent-mirror tree back onto the device (`--dry-run`, `--force`) |
| *(automatic)* | Every `put`/`install`/app upload is also archived on the Mac — see below |
| `install CAB` | Copy a `.cab` and launch the device installer (`wceload`) |
| `probe DEVICE [BAUD] [SECONDS]` | Sniff the serial line (no root) |
| `usb [pick\|pin PATH\|unpin]` | USB/Serial link status, auto-detected: the adapter and port carrying the link (`pick` for scripts, `pin` to override) |
| `db ls\|dump NAME\|snapshot NAME\|restore FILE` | Object-store databases: list, decode Pocket Outlook records, JSON snapshot and restore |
| `sync modules\|account …\|run NAME\|status NAME` | Two-way sync with modern apps and services — see [`docs/sync.md`](docs/sync.md) |
| `ppplog [FILE]` | Decode a `pppd` record file into readable PPP frames |

## Sync with modern apps

`jornada sync` (and the app's **Sync** pane) does what ActiveSync's sync
services did, with the Mac bridging everything the handheld cannot do itself:

- **Calendar, Contacts, Tasks** (Pocket Outlook) ⇄ Apple Calendar / Contacts /
  Reminders, Google, Microsoft 365, any CalDAV / CardDAV server, todo.txt.
- **Mail**: a POP3/SMTP bridge on the PPP link so the device's own Inbox works
  with IMAP accounts, Gmail, Microsoft 365 and Apple Mail.
- **Notes** (`.txt` on the device) ⇄ Apple Notes, Logseq, Bear.
- **Documents and spreadsheets** ⇄ Word / Excel on OneDrive, Google Docs /
  Sheets, Pages / Numbers; SQLite tables ⇄ Pocket Access.

```bash
./bin/jornada db ls                                          # what the object store holds
./bin/jornada sync account add home --module calendar --backend apple
./bin/jornada sync run home --dry-run                        # the plan, nothing written
./bin/jornada sync run home
```

A JSON snapshot of a device database is written before the first change of a
session, and `--direction from-device` keeps a run read-only on the handheld.
Set-up per backend, the wire-format caveats, and how to verify the record
layouts on your own device: [`docs/sync.md`](docs/sync.md).

## USB, the dock, and the 680e

The HP F1822A dock has a DB-9 and a USB-B jack, but the dock is passive and the
SH-3 Jornada 680/680e/690/690e has **no USB silicon**, so for those models the
USB jack carries nothing — the link runs over the DB-9 (or the sync cable)
through a USB-serial adapter. The StrongARM 720/728 do enumerate on it, as a
Windows CE USB Sync device macOS has no driver for. `jornada usb` (and the
app's **USB/Serial** pane) simply reports the status of the link — which adapter
is attached, which `/dev/cu.*` port it will open (chosen automatically: Apple's
driver over a vendor extension, a node you can actually open, a dock-built-in
bridge first), and how far the connection has come — flagging only real
problems. Background, sources, pinout notes, and the dock-bridge retrofit that
lets a 680e come up the dock's USB cable: [`docs/usb-link.md`](docs/usb-link.md).

## The macOS app

```bash
macapp/build.sh          # builds + signs "Jornada Sync.app" into ~/Applications
```

The app runs the protocol natively (Swift ports of the dccm listener and RAPI
client — no Python at runtime). **Connect** starts the PPP link via the system
administrator prompt; the Files pane supports drag-and-drop from Finder (onto
the folder being browsed, or onto any folder row to put files inside it),
dragging device files between folders to move them, download,
rename/delete/new-folder, and Run-on-device for `.exe`s.

## The sent-file archive

The Jornada's object store is battery-backed RAM, so **everything you send to
the device is automatically archived on the Mac too**, in a device-path-shaped
tree at `~/Documents/Jornada Backup/Sent to Device/` (override with
`$JORNADA_MIRROR_DIR`). Re-sending changed content never overwrites the
archive: the previous version is kept under a timestamped name, and every send
is recorded in `sent-manifest.jsonl` (time, device path, size, MD5 checksum,
source file). Skip it per command with `--no-mirror`. The CLI and the app write
the identical format — CI verifies the two implementations against each other.

To put it all back — after a battery death, a hard reset, or onto a second
device — point `restore` at either tree:

```bash
./bin/jornada restore ~/Documents/Jornada\ Backup/Sent\ to\ Device --dry-run
./bin/jornada restore ./jornada-backup
```

It recreates folders, skips files already on the device with a matching size
(`--force` resends everything), leaves manifests and timestamped archive
versions out, and warns if the plan exceeds the device's free space.

## Passwordless connect (optional)

Bringing up PPP needs root (that's a macOS requirement for `pppd`), so by
default Connect asks for your password. To skip the prompt from then on, run the
one-time installer (this single step authenticates):

```bash
sudo ./bin/jornada-setup-passwordless
```

It installs two small, **root-owned, argument-free** helper scripts and a
`sudoers` rule (`/etc/sudoers.d/jornada-link`) that grants passwordless `sudo`
**only** for those two commands. After that, Connect in the app and
`./bin/jornada-ppp` (no `sudo`) bring the link up with no prompt. To undo:

```bash
sudo ./bin/jornada-setup-passwordless --uninstall
```

This trades a little local security for convenience — see the
[security note](SECURITY.md#optional-passwordless-connect) before installing it
on a shared machine.

## How it works

- **Serial handshake.** The device repeats `CLIENT`; the host answers
  `CLIENTSERVER` and starts PPP. The Mac is `192.168.131.102`, the device
  `192.168.131.201`.
- **dccm (TCP 5679, device → host).** The device announces itself (OS version,
  build, name/class/hardware) and expects the host to answer its info packet
  with the ping word `0x12345678` and keep pinging, or it drops the link.
- **RAPI (TCP 990, host → device).** Length-prefixed `command + args` frames;
  the client wraps the CE file, directory, process, and system calls.

The wire format lives in one place per language — `jornada/wire.py` and
`macapp/Sources/JornadaCore/Wire.swift` — and both are verified against the same
in-memory fake device (`tests/fake_device.py`).

## Troubleshooting

- `./bin/jornada probe /dev/cu.usbserial-XXXX 115200 8` — sniff the line without
  root; you should see `CLIENT` while PC Link is trying.
- `pppd` logs to `~/.jornada-link/ppp.log`; raw serial traffic is recorded to
  `~/.jornada-link/ppp.record` (`./bin/jornada ppplog` decodes it).
- **"RAPI port not answering"**: the link is up but the device's file service
  isn't responding — reconnect PC Link, or soft-reset the Jornada (recessed
  Reset button; the object store survives) and reconnect.
- The Jornada auto-suspends on battery after a few idle minutes; keep it on AC
  for long sessions, and re-tap PC Link after it wakes.

## Security

The trust boundary is a physical point-to-point cable. The dccm listener only
accepts the device's PPP peer IP, RAPI frames are size-capped, device-supplied
names are neutralized before use as local paths, and the serial path is
validated before it reaches the root `pppd` command. The vintage protocol has no
transport encryption — treat the handheld as a trusted peer on a private cable,
not the link as confidential. Full threat model and OWASP mapping in
[`SECURITY.md`](SECURITY.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md). The whole
test suite runs against a fake device, so **you don't need a Jornada to
contribute**:

```bash
python3 -m pytest -q tests
```

## License

MIT — see [`LICENSE`](LICENSE). Protocol attribution and trademarks in
[`NOTICE.md`](NOTICE.md).
