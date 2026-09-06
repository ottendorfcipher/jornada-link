# AGENTS.md

Guidance for AI coding agents (and humans who like a terse reference) working in
this repo. Follows the [agents.md](https://agentsmd.net) convention. If anything
here conflicts with a maintainer instruction in a PR review, the maintainer
wins.

## What this project is

An interoperability tool that connects a modern Mac to a **vintage HP Jornada /
Windows CE 2.x** handheld over an FTDI serial cable. Two implementations of the
same 1990s wire protocols:

- `jornada/` — Python library + CLI. **Standard library only.**
- `macapp/` — Swift/SwiftUI macOS app. **Apple frameworks only.**

Keep both in sync when you touch the protocol.

## Setup / build / test commands

```bash
# Python (3.9+): the whole suite, no hardware or network needed
python3 -m pytest -q tests

# a single test
python3 -m pytest -q tests/test_rapi.py::test_upload_roundtrip_with_progress

# Swift (macOS 15+, Swift 6)
cd macapp && swift build
cd macapp && swift build --product SelfTest      # protocol self-test
cd macapp && ./build.sh                           # build+sign the .app
```

**Always run `python3 -m pytest -q tests` before proposing changes** and report
the pass/fail count. There is no network or hardware dependency — the suite runs
against `tests/fake_device.py`.

## Conventions (non-negotiable)

- **No new runtime dependencies.** Python = stdlib; Swift = Apple frameworks.
  This is a deliberate supply-chain property (see `SECURITY.md`), not an
  oversight.
- **One wire format, two languages.** `jornada/wire.py` and
  `macapp/Sources/JornadaCore/Wire.swift` must agree. A protocol change lands in
  both, plus a test in `tests/` and (if the client changed) the Swift
  `SelfTest`.
- **Immutability & small functions.** Prefer returning new values over mutating;
  keep functions focused and well-named.
- **Commits**: Conventional Commits (`feat:`, `fix:`, `security:`, `docs:`,
  `refactor:`, `test:`, `chore:`). Update `CHANGELOG.md` `[Unreleased]` for
  user-visible changes.

## Security boundaries — read before editing these

Anything crossing a trust boundary must be validated. In particular:

- **Device input is untrusted.** The far end of the cable may misbehave. RAPI
  reply frames are size-capped (`RAPI_MAX_FRAME`); device-supplied file names are
  neutralized before becoming local paths (`jornada/backup.py::_local_name` /
  `_safe_child`). Don't remove these guards.
- **The dccm listener** (`jornada/dccm.py`, `DccmListener.swift`) only accepts
  the device's PPP peer IP. Keep the allowlist.
- **The root PPP path** (`bin/jornada-ppp`, `PppController.swift`) runs as
  administrator. The serial device path is validated before interpolation into
  the privileged command — keep that validation, and never widen what the
  privileged helper touches beyond `~/.jornada-link` and system binaries.
- **Never log secrets.** The device password must not appear in logs or state
  files.

## Things not to do

- Don't add telemetry, network calls to third parties, or auto-download of
  device software. The tool copies user-supplied files only.
- Don't commit runtime artifacts: `~/.jornada-link/` state, `*.record` captures,
  `macapp/.build`, `macapp/dist`, coverage files (all git-ignored).
- Don't reintroduce the vintage `Linux`/JLime boot experiments — out of scope.

## Good first tasks

- Add a RAPI command already defined in the protocol notes but not yet wrapped
  (e.g. registry read) — with a fake-device handler and a test.
- Improve `jornada ppplog` decoding of additional PPP control frames.
- Broaden `tests/fake_device.py` error-path coverage.
