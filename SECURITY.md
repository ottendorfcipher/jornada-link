# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately using **GitHub's "Report a
vulnerability" flow** (repository → *Security* → *Advisories* → *Report a
vulnerability*). Do not open a public issue for a security problem.

Please include: what you observed, how to reproduce it, and the affected
version/commit. We aim to acknowledge within a few days. This is a
volunteer-maintained hobbyist project, so please set expectations accordingly;
there is no bounty.

## Supported versions

The project follows [Semantic Versioning](https://semver.org). While it is
pre-1.0 (`0.y.z`), only the latest released `0.y` line receives fixes.

## Threat model

`jornada-link` connects a modern Mac to a vintage Windows CE 2.x handheld over
a **point-to-point serial cable** carrying PPP. The trust boundary is small and
local:

- The **transport is a physical cable**, not a shared network. The realistic
  adversaries are (a) other hosts on any LAN/Wi-Fi the Mac is also attached to,
  who might reach the local listener; (b) a **malicious or malfunctioning device
  / RAPI implementation** on the far end of the cable feeding malformed frames;
  and (c) **local unprivileged processes** on the Mac racing the root helper.
- The tool requires **administrator rights only** to run `pppd` (which creates
  the network interface). Everything else runs as the normal user.
- The vintage protocols themselves offer **no transport encryption or strong
  authentication** — only an XOR-obfuscated device password. This is inherent to
  1990s Windows CE and cannot be fixed from this side. Do not treat the link as
  confidential; treat the handheld as a trusted peer on a private cable.

## Hardening in place (OWASP Top 10 : 2025)

| Category | How it is addressed |
|---|---|
| **A01 Broken Access Control** | The dccm listener (TCP 5679) accepts connections **only from the device's PPP peer IP** (`192.168.131.201`). It binds `0.0.0.0` because the PPP interface may not exist when it starts, so access is enforced by a peer allowlist, not the bind address. |
| **A02 Security Misconfiguration** | No services are exposed beyond the peer-filtered dccm port. `pppd` is invoked with an explicit, minimal option set (`noauth local nodefaultroute`, dead-peer detection). The root helper touches only `~/.jornada-link` and system binaries. |
| **A03 Software Supply Chain** | **Zero third-party runtime dependencies.** The Python package uses only the standard library; the Swift package uses only Apple frameworks. CI actions are pinned by commit SHA. |
| **A04 Cryptographic Failures** | The link is explicitly **not** treated as confidential (see threat model). The device password is sent using the only scheme CE understands (UTF‑16 XOR); it is never written to logs or state files. The backup manifest uses MD5 as a **non-security change-detection checksum**, not an integrity control. |
| **A05 Injection** | The serial device path is validated (`/dev/cu.*`, no shell metacharacters) before it reaches the root `pppd` command, since it may come from a user-writable pin file. Device-supplied file names are neutralized before use as local paths. |
| **A06 Vulnerable & Outdated Components** | Nothing to update — no dependency tree. Toolchain versions are documented in CI. |
| **A07 Identification & Authentication** | Optional device password is supported end-to-end; failures are reported, not silently ignored. The listener will not proceed past a rejected password. |
| **A08 Software & Data Integrity** | The tool does **not** download or execute remote code. Users supply their own `.cab`/app files; those are copied verbatim to the device and run **by the device's own installer** at the user's explicit request. |
| **A09 Logging & Alerting** | App and dccm events are logged to `~/.jornada-link/*.log`; **passwords are never logged**. Rejected peers and password failures are logged. Raw PPP recordings (`ppp.record`) stay local and are git-ignored. |
| **A10 Mishandling of Exceptional Conditions** | RAPI reply frames are capped (16 MB) so a hostile/buggy peer cannot drive unbounded allocation. Socket reads have timeouts; a wedged link fails fast with a clear message instead of hanging. Backup continues past individual unreadable entries and records the errors. |

## Optional: passwordless connect

`bin/jornada-setup-passwordless` is an **opt-in** convenience that removes the
per-connect admin prompt. It is designed to keep the privilege it grants as
narrow as possible:

- It installs two **root-owned** helper scripts (`jornada-connect` /
  `jornada-disconnect`) to `/usr/local/libexec/jornada-link/`, and a
  `sudoers.d` rule granting the invoking user passwordless `sudo` for **exactly
  those two commands** — nothing else.
- The helpers take **no arguments** (so the rule can't be widened at the call
  site), **never source or execute anything from a user-writable location**
  (their only config is the root-owned `/usr/local/etc/jornada-link/config`,
  which is parsed, not sourced), and **validate the serial device path** before
  it reaches `pppd`.
- The generated `sudoers` file is validated with `visudo -cf` before it is
  installed, and removed cleanly by `--uninstall`.

The trade-off: any process running as your user can now bring the PPP link up as
root without authenticating. On a single-user machine that is a small,
well-scoped increase in local attack surface; on a shared machine, prefer the
default admin prompt. Installing it still requires your password once.

## Residual risks (accepted)

- **No link confidentiality/authentication** beyond the CE password — inherent to
  the protocol; mitigated by the physical point-to-point cable.
- **Local privilege**: bringing up PPP needs `sudo`/admin. The helper script is
  generated in the user's own `~/.jornada-link` and validated, but any process
  already running as that user could tamper with it — this is the standard
  single-user desktop trust model.
