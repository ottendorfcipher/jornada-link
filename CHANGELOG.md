# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Sync** (`jornada sync`, the app's **Sync** pane): two-way synchronization of
  Pocket Outlook Calendar, Contacts and Tasks with Apple Calendar / Reminders /
  Contacts, Google Calendar / Contacts / Tasks, Microsoft 365 (Exchange Online,
  To Do) and any CalDAV / CardDAV server; a POP3/SMTP bridge that lets the
  device's own Inbox use IMAP accounts, Gmail, Microsoft 365 (XOAUTH2) and
  Apple Mail; notes (`.txt` on the device) with Apple Notes, Logseq and Bear;
  documents (`.txt`/`.rtf`) with Word on OneDrive, Google Docs and Pages;
  spreadsheets (`.csv`) with Excel on OneDrive, Google Sheets and Numbers; and
  SQLite tables with Pocket Access object-store databases. A three-way engine
  with content fingerprints, natural-key pairing on first sync, conflict
  preference, one-way modes and dry runs; JSON snapshots of a device database
  before its first write. Account settings under `~/.jornada-link/sync/`,
  secrets in 0600 files, OAuth with the user's own client registration.
  Safety rules: records a store cannot read are left alone on both sides
  (never treated as deletions), kept deletions stay linked, recurring
  appointments are never rewritten, remote deletions go to a trash where the
  service has one, and the device database is snapshotted before its first
  write. See `docs/sync.md`.

- **Object-store databases** (`jornada db ls|dump|snapshot|restore`): the RAPI
  database calls (`CeFindAllDatabases`, `CeOpenDatabase`, `CeCreateDatabase`,
  `CeDeleteDatabase`, `CeReadRecordProps`, `CeWriteRecordProps`,
  `CeDeleteRecord`, `CeSeekDatabase`) in the librapi2 0.9.x wire format, a
  CEPROPVAL record codec, and Pocket Outlook record codecs from the SynCE
  property tables — in Python and Swift, verified against each other through
  the fake device (`SelfTest rapi`, `SelfTest cedb`).

- **Drag-and-drop into folders** in the app's Files pane: drop Finder files
  onto a folder row to upload them *into* that folder (dropping on blank space
  still targets the folder being browsed), and drag device files or folders
  onto another folder to move them on the Jornada. Folder rows highlight while
  targeted, a selected row drags the whole selection, moving a folder into
  itself is refused, and every operation refreshes the listing so the app
  reflects the device immediately.

### Changed

- **USB pane condensed to "USB/Serial"** — one status card (adapter, port,
  link state, plus a single guidance line only when the doctor finds a real
  problem), everything auto-detected. The handheld-model picker, per-device
  tables, per-node pin buttons and the dock essay are gone from the app; the
  port is chosen automatically. `jornada usb` shrinks to match: `status`
  (default, `--json`), `pick`, `pin`, `unpin` — `doctor`, `list`, `profiles`
  and `--model` are removed. The detection engine and its Swift/Python parity
  check are unchanged.

### Fixed

- **`settime` / Set Clock now syncs the date correctly.** It pushes the Mac's
  local wall-clock (not UTC), so the Jornada reads the same date and time you
  see on the Mac; previously the device kept UTC and showed tomorrow's date in
  the evening. Both the CLI and the app now read the clock back and report the
  device's resulting date and time. `settime --utc` sends UTC for devices whose
  own time-zone is configured.

### Added

- **USB link module** (`jornada usb`, the app's **USB** pane): a driver table of
  the USB devices that can carry a link (FTDI/Prolific/Silicon Labs/WCH bridges,
  CDC-ACM, and the Windows CE USB Sync function devices of the StrongARM
  Jornadas and Pocket PCs), a handheld capability matrix for the F1822A dock,
  I/O Registry enumeration that reports every `/dev/cu.*` node with its owning
  driver and whether the user can open it, and a doctor that ranks the nodes,
  pins one, and explains the findings — including why the dock's USB-B jack is
  inert for the SH-3 680/680e/690/690e. `Connect` and `bin/jornada-ppp` now use
  the doctor's pick (pin file still wins). Same table in Python and Swift, with a
  CI parity check. Research, sources and the dock-bridge retrofit that lets a
  680e use the dock's USB cable: `docs/usb-link.md`.

- **`jornada rm -r`**: recursively delete a device directory and its contents
  (depth-first: files, then directories leaf-up), collecting per-entry errors;
  refuses the device root as a guardrail. Enables cleanup of restored/test trees
  the one-file-at-a-time RAPI protocol otherwise makes tedious.

- **`jornada restore`**: push a `backup` or sent-mirror tree back onto the
  device — recreates directories, skips name+size matches (`--force` overrides),
  excludes manifests and timestamped archive versions, previews with
  `--dry-run`, and warns when the plan exceeds the device's free object store.

- **Sent-file archive**: every successful `put`, `install`, and app upload is
  also saved on the Mac under `~/Documents/Jornada Backup/Sent to Device/`
  (device-path-shaped tree, timestamped versioning of changed re-sends,
  append-only `sent-manifest.jsonl` with sizes and checksums). Opt out per
  command with `--no-mirror`, relocate with `$JORNADA_MIRROR_DIR`. Implemented
  identically in Python and Swift; a CI parity check validates the Swift
  writer's output against the Python implementation's expectations.

- **Optional passwordless connect** (`bin/jornada-setup-passwordless`): a
  one-time installer that places two root-owned, argument-free helper scripts
  (`jornada-connect` / `jornada-disconnect`) and a narrowly scoped `sudoers.d`
  NOPASSWD rule, so the app's Connect and `bin/jornada-ppp` no longer prompt for
  a password. The app and CLI prefer the helper when installed and fall back to
  the admin prompt otherwise. `--uninstall` removes it. See
  [`SECURITY.md`](SECURITY.md#optional-passwordless-connect) for the trade-off.

## [0.1.0] — 2026-08-24

First public release. Connects a modern Mac to an HP Jornada / Windows CE 2.x
handheld over an FTDI serial cable, with no software installed on the device.

### Added

- **Python library + CLI** (`jornada/`, `bin/jornada`) speaking the Windows CE
  ActiveSync-era protocols directly (stdlib only, no dependencies):
  - Serial line probe, PPP handshake wrapper (`bin/jornada-ppp`), and a decoder
    for `pppd` record files (`jornada ppplog`).
  - dccm connection notifier (TCP 5679) and RAPI client (TCP 990).
  - Commands: `status`, `ls`, `get`, `put`, `rm`, `mkdir`, `rmdir`, `mv`, `run`,
    `settime`, `shortcut`, `backup`, `install`, `probe`, `ppplog`.
  - Recursive device `backup` with a JSON manifest.
- **Jornada Sync.app** — a native SwiftUI macOS app (Apple frameworks only)
  styled after Microsoft ActiveSync: device banner, Overview, drag-and-drop file
  browser, transfers, and a log viewer, with a drawn-in-code logo and `.icns`.
- **Test suite** — 60 pytest cases plus an in-memory fake device, and a Swift
  `SelfTest` binary that verifies the Swift client against the same fake.

### Security

- OWASP Top 10 (2025) hardening pass:
  - dccm listener restricted to the device's PPP peer IP (A01/A02).
  - RAPI reply frames capped at 16 MB against unbounded allocation (A10).
  - Device-supplied names neutralized before use as local paths (A01/A05).
  - Serial device path validated before reaching the root `pppd` command (A05).
- See [`SECURITY.md`](SECURITY.md) for the full threat model.

[Unreleased]: https://github.com/ottendorfcipher/jornada-link/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ottendorfcipher/jornada-link/releases/tag/v0.1.0
