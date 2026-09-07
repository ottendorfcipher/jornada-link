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

## Backends and their settings

The authoritative list is `jornada sync modules`; this is its output at the
time of writing. Settings marked *secret* are stored in the secret file, never
in `accounts.json`; *optional* ones have the default shown.

```
calendar: Calendar (Pocket Outlook Appointments) (two-way store)
    device setting window_past: days back to sync (default 30) (optional)
    device setting window_future: days ahead (default 365) (optional)
    device setting recurring: remote recurring events: skip (default) or first (sync the master as a one-off) (optional)
  apple: Apple Calendar (Calendar.app)
      calendar: calendar in Calendar.app to sync (default Jornada; created when missing) [optional]
      note: Drives Calendar.app through osascript; macOS asks for Automation permission on the first run. Busy status, privacy and categories are not scriptable: they stay at their defaults in Calendar.
  google: Google Calendar
      client_id: OAuth client id of your own Google app registration
      client_secret: OAuth client secret (Google desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      calendar_id: calendar id to sync (default primary; other calendars have ids like abc@group.calendar.google.com) [optional]
      note: Needs your own Google Cloud OAuth desktop client (calendar.events scope). Categories and the tentative / out-of-office states are kept in the event's private extended properties.
  m365: Microsoft 365 / Exchange Online calendar
      client_id: OAuth client id of your own Microsoft app registration
      client_secret: OAuth client secret (Microsoft desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      tenant: Azure AD tenant id or domain (default common) [optional]
      calendar: calendar display name (default: the mailbox's default calendar) [optional]
      note: Needs an Azure app registration (public client) with Calendars.ReadWrite. Event bodies are flattened to plain text on the way to the device.
  caldav: CalDAV (Fastmail, Nextcloud, iCloud, Radicale, …)
      url: server or calendar URL (https://…; calendar discovery starts there)
      username: user name
      password: password or app-specific password [secret]
      calendar: calendar display name or href (default: the first calendar that holds events) [optional]
      note: Writes are conditional on ETags, so a change made on the server between listing and writing is reported instead of overwritten. Attendees, attachments and other properties this tool does not model are dropped when an event is rewritten from the device.
contacts: Contacts (Pocket Outlook Contacts) (two-way store)
  apple: Apple Contacts (Contacts.app)
      group: Contacts group to sync (default Jornada; created if missing; * means every contact) [optional]
      note: Drives Contacts.app through osascript; macOS asks for Automation permission on the first run (notes need a separate permission and are skipped without it). Office, categories, anniversary, spouse, children and assistant have no place in Contacts and do not travel; an update replaces a person's e-mails, phones, addresses and web page with the device's.
  google: Google Contacts
      client_id: OAuth client id of your own Google app registration
      client_secret: OAuth client secret (Google desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      group: contact group (label) to sync; only its members take part and new contacts join it (default: every contact) [optional]
      note: Needs your own Google Cloud OAuth desktop client with the contacts scope. Google has no categories, so the device's categories do not travel; a birthday without a year is ignored.
  m365: Microsoft 365 contacts
      client_id: OAuth client id of your own Microsoft app registration
      client_secret: OAuth client secret (Microsoft desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      tenant: Azure AD tenant id or domain (default common) [optional]
      folder: contact folder display name (default: the mailbox's own contacts) [optional]
      note: Needs an Azure app registration (public client, redirect http://localhost) with Contacts.ReadWrite. Outlook has no fax or pager fields: a work fax takes a free business phone slot (a home fax a free home slot) and reads back as a second number; pager, car, radio and assistant numbers do not travel.
  carddav: CardDAV (Fastmail, Nextcloud, iCloud, …)
      url: server or address book URL (https://…; discovery starts there)
      username: user name
      password: password or app-specific password [secret]
      addressbook: address book display name or href (default: the first one) [optional]
      note: Writes are conditional on ETags, so a change made on the server between listing and writing is reported instead of overwritten. Photos, nicknames, instant-messaging handles and other properties this tool does not model are dropped when a card is rewritten from the device.
documents: Documents and spreadsheets (two-way store)
    device setting folder: device folder to sync (default \My Documents) (optional)
  word: Microsoft Word documents on OneDrive
      client_id: OAuth client id of your own Microsoft app registration
      client_secret: OAuth client secret (Microsoft desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      tenant: Azure AD tenant for sign-in (default common) [optional]
      path: OneDrive folder path (default Documents/Jornada) [optional]
      device_format: file type written to the device: txt (default, Pocket Word ANSI text) or rtf [optional]
      note: Register a public client (mobile/desktop, redirect http://localhost) with the Files.ReadWrite permission; documents travel as plain paragraphs in .docx, and .txt files in the folder are read too.
  gdocs: Google Docs
      client_id: OAuth client id of your own Google app registration
      client_secret: OAuth client secret (Google desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      folder_id: Drive folder id to sync (default: a folder named folder_name under My Drive) [optional]
      folder_name: name of the Drive folder to find or create (default Jornada) [optional]
      device_format: file type written to the device: txt (default, Pocket Word ANSI text) or rtf [optional]
      note: Uses the drive.file scope, which only sees files this tool created or that you opened with it: Docs written elsewhere in Drive stay invisible until they are recreated through the sync. Documents are exchanged as plain text (Drive converts uploads into Docs).
  pages: Apple Pages files in a folder
      path: Mac folder holding the .pages documents (created on first write)
      device_format: file type written to the device: txt (default, Pocket Word ANSI text) or rtf [optional]
      note: Drives Pages through Automation (allow it when macOS asks); deleted documents go to the Trash. The device folder setting is `folder`; `path` is the Mac folder.
  excel: Microsoft Excel workbooks on OneDrive
      client_id: OAuth client id of your own Microsoft app registration
      client_secret: OAuth client secret (Microsoft desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      tenant: Azure AD tenant of the app registration (default common) [optional]
      path: OneDrive folder holding the workbooks (default Documents/Jornada) [optional]
      device_format: file type written to the device: csv (default) or tsv (tab-separated .txt, which Pocket Excel opens directly) [optional]
      note: Each workbook's first worksheet is synced; .csv files in the folder are synced too.
  sheets: Google Sheets
      client_id: OAuth client id of your own Google app registration
      client_secret: OAuth client secret (Google desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      folder_id: Drive folder id holding the spreadsheets (found by name when unset) [optional]
      folder_name: Drive folder name, found or created under My Drive (default Jornada) [optional]
      note: Each spreadsheet's first sheet is synced as one CSV sheet.
  numbers: Apple Numbers files in a folder
      folder: Mac folder holding the .numbers documents
      note: Numbers must be installed; each document's first table is synced through CSV export/import.
  sqlite: SQLite database tables (Pocket Access)
      database: path of the SQLite file (created when missing)
      tables: comma-separated table names to sync (default: every table) [optional]
      note: Each table becomes a Windows CE database on the device, as ActiveSync did for Pocket Access.
mail: Mail (Inbox bridge for the device's own mail client) (bridge)
    device setting listen: IP address the bridge listens on (default: the Mac's PPP address 192.168.131.102) (optional)
    device setting pop3_port: POP3 port the device connects to (default 110; ports below 1024 need sudo — use 1110 here and in the device's service settings instead) (optional)
    device setting smtp_port: SMTP port the device connects to (default 25; ports below 1024 need sudo — use 1025 here and on the device instead) (optional)
    device setting smtp_auth: require the local user name and password for outgoing mail: on (default) or off (the bridge only listens on the PPP address, so off is acceptable for an Inbox that cannot authenticate) (optional)
    device setting allow_any_interface: yes to permit listen=0.0.0.0 (exposes the bridge and your mail to the whole network; default no) (optional)
  imap: Any IMAP account (password)
      host: IMAP server, e.g. imap.example.org
      port: IMAP port (default 993 for ssl, 143 for starttls) [optional]
      security: how IMAP is encrypted: ssl (default) or starttls [optional]
      username: IMAP user name (usually the e-mail address)
      password: IMAP password (an app password where the provider issues them) [secret]
      mailbox: mailbox offered to the device (default INBOX) [optional]
      outgoing_host: SMTP server for sending (default: the IMAP host, imap. replaced by smtp.) [optional]
      outgoing_port: SMTP port (default 587 for starttls, 465 for ssl) [optional]
      outgoing_security: how SMTP is encrypted: starttls (default) or ssl [optional]
      outgoing_username: SMTP user name when it differs from username [optional]
      outgoing_password: SMTP password when it differs from password [secret, optional]
      local_user: user name the device's Inbox logs in with (any ASCII word, e.g. jornada)
      local_password: password the device's Inbox logs in with (ASCII; not your real password) [secret]
      max_messages: newest N messages offered to the device (default 50) [optional]
      max_size: messages larger than this many bytes reach the device as a short stub (default 262144) [optional]
      on_delete: what deleting on the device does to the real message: keep (default), delete or archive [optional]
      archive_mailbox: folder used by on_delete=archive (default Archive) [optional]
      note: TLS only (IMAP over SSL or STARTTLS, SMTP STARTTLS or SSL) with certificate verification; providers that require OAuth are covered by the gmail and m365 backends.
  gmail: Gmail (XOAUTH2)
      client_id: OAuth client id of your own Google app registration
      client_secret: OAuth client secret (Google desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      email: the Google address (also the XOAUTH2 user)
      mailbox: mailbox offered to the device (default INBOX) [optional]
      local_user: user name the device's Inbox logs in with (any ASCII word, e.g. jornada)
      local_password: password the device's Inbox logs in with (ASCII; not your real password) [secret]
      max_messages: newest N messages offered to the device (default 50) [optional]
      max_size: messages larger than this many bytes reach the device as a short stub (default 262144) [optional]
      on_delete: what deleting on the device does to the real message: keep (default), delete or archive [optional]
      archive_mailbox: folder used by on_delete=archive (default [Gmail]/All Mail) [optional]
      note: enable IMAP in Gmail settings; the OAuth client needs the https://mail.google.com/ scope. on_delete=archive removes the Inbox label (copy to [Gmail]/All Mail), on_delete=delete moves to [Gmail]/Trash.
  m365: Microsoft 365 (XOAUTH2)
      client_id: OAuth client id of your own Microsoft app registration
      client_secret: OAuth client secret (Microsoft desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      email: the Microsoft address (also the XOAUTH2 user)
      mailbox: mailbox offered to the device (default INBOX) [optional]
      tenant: Entra ID tenant id or domain (default common) [optional]
      local_user: user name the device's Inbox logs in with (any ASCII word, e.g. jornada)
      local_password: password the device's Inbox logs in with (ASCII; not your real password) [secret]
      max_messages: newest N messages offered to the device (default 50) [optional]
      max_size: messages larger than this many bytes reach the device as a short stub (default 262144) [optional]
      on_delete: what deleting on the device does to the real message: keep (default), delete or archive [optional]
      archive_mailbox: folder used by on_delete=archive (default Archive) [optional]
      note: the app registration needs the IMAP.AccessAsUser.All and SMTP.Send delegated permissions, and authenticated SMTP must be enabled for the mailbox.
  apple: Apple Mail (Mail.app on this Mac)
      account: Mail account name (default: every account) [optional]
      mailbox: mailbox offered to the device (default INBOX) [optional]
      sender: From address for mail sent from the device (default: Mail's own default) [optional]
      local_user: user name the device's Inbox logs in with (any ASCII word, e.g. jornada)
      local_password: password the device's Inbox logs in with (ASCII; not your real password) [secret]
      max_messages: newest N messages offered to the device (default 50) [optional]
      max_size: messages larger than this many bytes reach the device as a short stub (default 262144) [optional]
      on_delete: what deleting on the device does to the real message: keep (default), delete or archive [optional]
      archive_mailbox: folder used by on_delete=archive (default Archive) [optional]
      note: needs Automation permission for Mail; messages reach the device as plain text without attachments.
notes: Notes (two-way store)
    device setting folder: device folder of .txt notes (default \My Documents\Notes) (optional)
  apple: Apple Notes
      notes_folder: folder in Notes to sync (default Jornada; created if missing) [optional]
      account: Notes account holding the folder (default: the default account) [optional]
      note: Drives the Notes app through osascript; macOS asks for Automation permission on the first run.
  logseq: Logseq graph
      graph: path of the Logseq graph folder on this Mac
      kind: pages (default) or journals [optional]
      tag: only sync pages carrying #tag or tags:: tag (added to pages written by the sync) [optional]
      note: Edits the Markdown files directly; Logseq picks the changes up while it is open.
  bear: Bear
      database: path of Bear's database.sqlite (default: Bear's own, read-only) [optional]
      tag: only sync notes carrying #tag (added to notes written by the sync) [optional]
      note: Reads Bear's database; writes go through bear:// URLs, so Bear must be running during a sync.
tasks: Tasks (Pocket Outlook Tasks) (two-way store)
    device setting completed: completed tasks: keep (default: they sync like open ones) or hide (they are left out of both sides, so they are never created on the other side; a task that disappears from one side is marked completed on the other instead of deleted, and the pair is then unlinked) (optional)
  apple: Apple Reminders (Reminders.app)
      list: reminder list to sync (default Jornada; created if missing) [optional]
      note: Drives Reminders through osascript; macOS asks for Automation permission on the first run. Reminders has no categories, start dates or private flag, so those fields do not travel.
  mstodo: Microsoft To Do
      client_id: OAuth client id of your own Microsoft app registration
      client_secret: OAuth client secret (Microsoft desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      tenant: Azure sign-in tenant: common (default), consumers, organizations or a tenant id [optional]
      list: To Do list to sync, by name (default: the built-in Tasks list) [optional]
      note: Needs an Azure app registration (public client, redirect http://localhost) with Tasks.ReadWrite. The private flag does not travel.
  gtasks: Google Tasks
      client_id: OAuth client id of your own Google app registration
      client_secret: OAuth client secret (Google desktop clients have one) [secret, optional]
      token: sign-in token (filled by `jornada sync account login`) [secret]
      tasklist: task list to sync, by title (default @default: the primary list) [optional]
      note: Google Tasks has no priorities, categories, start dates or private flag: those fields do not travel and every task reads back with priority normal.
  caldav: CalDAV tasks (VTODO: Nextcloud Tasks, Tasks.org, Radicale…)
      url: server or calendar URL (https://cloud.example.com/remote.php/dav/ …)
      username: login name
      password: password or app password [secret]
      calendar: calendar to sync, by display name or href (default: the first one that accepts tasks) [optional]
      note: Tasks are exchanged as VTODO objects; recurrence rules, alarms and other properties of an existing object are preserved on update.
  todotxt: todo.txt or Markdown checklist file
      path: the file to sync (created if missing)
      format: todotxt (default) or markdown (a checklist with Obsidian Tasks fields) [optional]
      done_path: optional second file for completed tasks (todo.txt's done.txt); open tasks stay in path [optional]
      note: One task per line: notes and the private flag do not fit and do not travel; categories become +project/@context (todo.txt) or #tags (Markdown). A line gets its id: tag (🆔) the first time the sync writes the file; until then an edited line counts as a new task.
```

## Limitations

- Recurring appointments are read from the device but never written or
  modified (the recurrence blob is not modelled); recurring remote events are
  synced only as the master record. 
- Pocket Word `.pwd` and Pocket Excel `.pxl` binaries are not converted; use
  `.txt`/`.rtf` and `.csv` on the device.
- Contact notes on macOS need the Contacts notes entitlement; the app skips
  them when it is not granted.
