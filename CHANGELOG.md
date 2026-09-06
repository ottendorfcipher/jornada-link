# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
