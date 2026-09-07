# Sync: Pocket Outlook, notes and documents ⇄ modern apps

`jornada sync` (and the app's **Sync** pane) keep the Jornada's data in step with
the apps and services you use today. It plays the role ActiveSync's sync
services played in 1999, with the Mac doing everything the handheld cannot:
TLS, OAuth, modern file formats.

| Module | On the device | Backends |
|---|---|---|
| `calendar` | Pocket Outlook Calendar (the *Appointments Database*) | Apple Calendar, Google Calendar, Microsoft 365 / Exchange Online, any CalDAV server |
| `contacts` | Pocket Outlook Contacts (*Contacts Database*) | Apple Contacts, Google Contacts, Microsoft 365, any CardDAV server |
| `tasks` | Pocket Outlook Tasks (*Tasks Database*) | Apple Reminders, Microsoft To Do, Google Tasks, CalDAV tasks (VTODO), todo.txt and Markdown checklists |
| `mail` | The device's own Inbox client, through a local POP3/SMTP bridge | Any IMAP account, Gmail, Microsoft 365, Apple Mail |
| `notes` | A folder of `.txt` notes (Pocket Word, HP Quick Pad) | Apple Notes, Logseq, Bear |
| `documents` | Pocket Word (`.txt`/`.rtf`), Pocket Excel (`.csv`), Pocket Access tables | Word on OneDrive, Google Docs, Apple Pages, Excel, Google Sheets, Apple Numbers, SQLite |

## How it works

```
 device database ──RAPI CeReadRecordProps──► decode ──► neutral record ─┐
                                                                         ├─► three-way plan ─► apply
 Google / CalDAV / Apple app ──HTTPS or AppleScript──► neutral record ──┘        │
                                                              state file ◄───────┘
```

- **The device side** is read and written with the RAPI database calls
  (`CeFindAllDatabases`, `CeOpenDatabase`, `CeReadRecordProps`,
  `CeWriteRecordProps`, `CeDeleteRecord`) in the librapi2 wire format; the
  Pocket Outlook record layouts and property ids come from the SynCE project,
  which established them against real Windows CE devices (`jornada/pim/ids.py`).
- **Neutral records** (`jornada/pim/models.py`) are what both sides speak:
  `Appointment`, `Contact`, `Task`, `Note`, `Document`. Each has a content
  fingerprint, so change detection needs no modification stamps.
- **The engine** (`jornada/sync/engine.py`) compares both listings with the
  saved state (`~/.jornada-link/sync/state/<module>-<account>.json`): a record
  whose fingerprint differs from the one in its link changed on that side.
  Changes flow to the other side; a record changed on both sides follows
  `--prefer` (default: the remote copy wins, the device copy is in the
  snapshot); records never seen before are paired by a natural key (subject
  and start, name and e-mail, …) so a first sync of two populated sides does
  not duplicate everything. `--direction to-device|from-device` makes a run
  one-way, `--no-delete` stops deletions from propagating.
- **Safety net.** Before the first write of a session the whole device
  database is saved as JSON under `~/Documents/Jornada Backup/PIM Snapshots/`;
  `jornada db restore FILE` pushes it back. Every file written to the device
  is also archived in the sent-file mirror. `--dry-run` prints the plan and
  changes nothing.

- **What never happens by accident.** A record a store cannot read (a corrupt
  date, a file that is not what its extension says, a Bear note that is
  encrypted) is listed as *unreadable* and left alone on both sides rather than
  being read as a deletion. With `--no-delete`, or in a one-way run, a record
  deleted on one side keeps its link so the surviving copy is neither deleted
  nor recreated. Recurring appointments on the device are never rewritten or
  deleted. Remote deletions go to a recycle bin where the service has one
  (Drive's Trash, OneDrive's recycle bin, Apple's Trash), to
  `logseq/bak/jornada/` in a Logseq graph, and to a `_jornada_trash_` table in
  SQLite. The state file is saved right after the changes are applied, before
  the stores are re-read.

Times: the Jornada has one clock and `jornada settime` sets it to the Mac's
local wall-clock, so device times are treated as local wall-clock values and
converted to the Mac's time zone on the modern side.

## Commands

```bash
jornada db ls                               # every object-store database, with record counts
jornada db dump "Contacts Database"         # decoded records (--raw for every property, --json)
jornada db snapshot "Appointments Database" # JSON copy under the Jornada Backup folder
jornada db restore SNAPSHOT.json            # push a snapshot back (records are added, not merged)

jornada sync modules                        # modules, backends and their settings
jornada sync account add NAME --module calendar --backend caldav \
    --set url=https://dav.example.org/ --set username=me --ask password
jornada sync account login NAME             # browser sign-in for Google / Microsoft backends
jornada sync run NAME --dry-run             # show the plan
jornada sync run NAME                       # sync
jornada sync run NAME --direction to-device --prefer local --no-delete
jornada sync status NAME
jornada sync account list | remove NAME
```

Settings that are secrets (passwords, tokens, client secrets) never go into
`accounts.json`; they live in `~/.jornada-link/sync/secrets/<account>.json`
with mode 0600. Use `--ask key` to type one without echo, or `--set key=value`.

Google and Microsoft backends use OAuth with **your own** app registration
(no shared client id is built in): create a "Desktop app" OAuth client in
Google Cloud Console (client id + client secret) or a public-client app
registration in Entra ID / Azure with the scopes the backend lists, then
`--set client_id=… --set client_secret=…` (Google) and `account login`.

## The mail bridge

The device's Inbox speaks plain POP3 and SMTP, which no 2026 mail server
accepts. `jornada sync run <mail account>` therefore starts a **bridge**: a
POP3 server and an SMTP server on the Mac's PPP address (192.168.131.102)
that the device connects to with a local user name and password, while the
Mac talks to the real account over TLS (IMAP/SMTP, or XOAUTH2 for Gmail and
Microsoft 365) or drives Mail.app. On the Jornada, create an Inbox service with
the Mac's address as both the incoming (POP3) and outgoing (SMTP) server and
the local credentials. Ports below 1024 need root, so the defaults can be
changed to 1110/1025 on both ends.

## Verifying against the real device

The database wire format and the Pocket Outlook record layouts were taken
from SynCE's librapi2 and librra sources, which were validated against
Windows CE devices but not against this exact 680e. Before trusting a
two-way sync:

1. `jornada db ls` — the three Pocket Outlook databases must appear with
   plausible record counts.
2. `jornada db dump "Contacts Database" --raw` — every record should decode;
   strings must be readable and property ids match `jornada/pim/ids.py`.
3. Create one appointment (timed), one all-day appointment, one task and one
   contact on the device; `dump --raw` them and compare with what
   `jornada/pim/appointments.py` etc. expect (in particular the all-day
   duration convention).
4. `jornada sync run NAME --direction from-device --dry-run` (read-only) before
   any run that writes to the device.

If a layout differs, the codecs in `jornada/pim/` are the single place to
adjust; the Swift app shares the same tables.

## Limitations

- Recurring appointments are read from the device but never written or
  modified (the recurrence blob is not modelled); recurring remote events are
  synced only as the master record. 
- Pocket Word `.pwd` and Pocket Excel `.pxl` binaries are not converted; use
  `.txt`/`.rtf` and `.csv` on the device.
- Contact notes on macOS need the Contacts notes entitlement; the app skips
  them when it is not granted.
