# Contributing to jornada-link

Thanks for your interest! This is a hobbyist interoperability project for
vintage HP Jornada / Windows CE 2.x handhelds. Contributions of all sizes are
welcome — bug fixes, new RAPI commands, docs, or testing against hardware we
don't have.

## Ground rules

- **Be excellent to each other.** Assume good faith; keep discussion technical.
- **You don't need the hardware to contribute.** The test suite runs entirely
  against an in-memory fake device (`tests/fake_device.py`), so most changes can
  be developed and verified with no Jornada attached.
- **Interoperability only.** This project talks to hardware its owners already
  possess. Please don't add anything designed to attack or exfiltrate from
  devices you don't own.

## Project layout

```
jornada/        Python library + CLI (stdlib only, no dependencies)
bin/            jornada (CLI launcher), jornada-ppp (root PPP wrapper)
macapp/         Swift/SwiftUI macOS app "Jornada Sync" (Apple frameworks only)
tests/          pytest suite + the in-memory fake device
```

See [`AGENTS.md`](AGENTS.md) for a concise build/test/convention reference
(useful for humans and AI coding agents alike).

## Development setup

Python side (3.9+):

```bash
python3 -m pytest -q tests          # run the whole suite
python3 -m pytest -q tests/test_rapi.py::test_download_small_and_chunked
```

Swift side (macOS 15+, Swift 6 toolchain):

```bash
cd macapp
swift build
swift build --product SelfTest      # protocol self-test binary
./build.sh                          # assemble + sign Jornada Sync.app
```

Cross-language protocol check — the Swift client is verified against the same
Python fake device the unit tests use:

```bash
# terminal 1: start a populated fake device, note the printed port
python3 tests/serve_fake.py
# terminal 2:
macapp/.build/debug/SelfTest rapi <port>
```

## Coding conventions

- **Python**: standard library only — do not add runtime dependencies. Prefer
  small, immutable, well-named functions. Keep the wire format in
  `jornada/wire.py` the single source of truth.
- **Swift**: Apple frameworks only, no SPM dependencies. Mirror the Python wire
  semantics in `macapp/Sources/JornadaCore/Wire.swift`.
- **Any protocol change must be reflected in both languages** and covered by a
  test in `tests/` (and, where it touches the client, the Swift `SelfTest`).
- **Security**: validate anything that crosses a trust boundary (device input,
  the serial-path pin file, user-supplied paths). See [`SECURITY.md`](SECURITY.md).

## Commit and PR conventions

- Commits follow [Conventional Commits](https://www.conventionalcommits.org):
  `feat:`, `fix:`, `security:`, `docs:`, `refactor:`, `test:`, `chore:`.
- One logical change per PR. Include *why*, not just *what*.
- **Run `python3 -m pytest -q tests` before pushing** and mention the result.
- If you tested against real hardware, say which model and CE version.
- Update [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]` for user-visible
  changes.

## Versioning

The project uses [Semantic Versioning](https://semver.org). It is pre-1.0, so
the public API (CLI flags, library signatures) may still change between `0.y`
releases; breaking changes bump the minor while `0.`.

## AI-assisted contributions

AI-assisted PRs are welcome, with the same bar as any other: **you are the
author and are responsible for the result.** Read and understand the diff,
verify it against the test suite, and disclose significant AI assistance in the
PR description. See [`AGENTS.md`](AGENTS.md) for agent guidance. Do not paste
untrusted device output or logs into prompts without review.
