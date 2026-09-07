"""Start a populated in-memory fake Jornada and print its RAPI port.

Used by CI and by contributors to exercise the Swift client (SelfTest) or the
CLI without real hardware. Prints the chosen TCP port on the first line, then
serves until the timeout elapses or the process is killed.

Usage:  python3 tests/serve_fake.py [seconds]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jornada.cedb import PropVal  # noqa: E402
from jornada.pim import ids  # noqa: E402
from tests.fake_cedb import FakeDatabaseStore  # noqa: E402
from tests.fake_device import FakeFilesystem, FakeRapiServer  # noqa: E402

# Pocket Outlook sample records (property ids from jornada/pim/ids.py). The
# Swift SelfTest asserts these exact values, so change them together.
APPOINTMENTS = [
    (PropVal.string(ids.SUBJECT, "Dentist"), PropVal.filetime(ids.APPT_START, 0x01D9E0F09B2C3D4E),
     PropVal.i4(ids.APPT_DURATION, 30), PropVal.i4(ids.APPT_TYPE, ids.APPT_TYPE_NORMAL),
     PropVal.i2(ids.APPT_OCCURRENCE, ids.OCCURRENCE_ONCE), PropVal.i2(ids.APPT_BUSY_STATUS, ids.BUSY_BUSY),
     PropVal.blob(ids.NOTES, b"bring card\r\n")),
    (PropVal.string(ids.SUBJECT, "Standup"), PropVal.string(ids.APPT_LOCATION, "Room 4"),
     PropVal.filetime(ids.APPT_START, 0x01D9E0F1_0000_0000), PropVal.i4(ids.APPT_DURATION, 15),
     PropVal.i4(ids.APPT_TYPE, ids.APPT_TYPE_NORMAL), PropVal.i2(ids.APPT_OCCURRENCE, ids.OCCURRENCE_ONCE)),
]
CONTACTS = [
    (PropVal.string(ids.CONTACT_FIRST_NAME, "Ada"), PropVal.string(ids.CONTACT_LAST_NAME, "Lovelace"),
     PropVal.string(ids.CONTACT_FULL_NAME, "Ada Lovelace"), PropVal.string(ids.CONTACT_EMAIL, "ada@example.org"),
     PropVal.string(ids.CONTACT_MOBILE_TEL, "+44 20 7946 0001"), PropVal.string(ids.CONTACT_CATEGORIES, "Friends")),
]
TASKS = [
    (PropVal.string(ids.SUBJECT, "Buy batteries"), PropVal.filetime(ids.TASK_DUE, 0x01D9E0F2_0000_0000),
     PropVal.i4(ids.IMPORTANCE, ids.IMPORTANCE_NORMAL)),  # TASK_COMPLETED deliberately absent
]


def seed_filesystem() -> FakeFilesystem:
    filesystem = FakeFilesystem()
    filesystem.dirs.update({"\\My Documents", "\\Windows"})
    filesystem.files["\\My Documents\\notes.txt"] = b"hello jornada\n"
    filesystem.files["\\Windows\\pword.exe"] = b"MZ fake"
    return filesystem


def seed_databases(store: FakeDatabaseStore) -> None:
    store.create(ids.DB_APPOINTMENTS, APPOINTMENTS, db_type=0)
    store.create(ids.DB_CONTACTS, CONTACTS)
    store.create(ids.DB_TASKS, TASKS)


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    server = FakeRapiServer(seed_filesystem())
    seed_databases(server.db)
    server.start()
    print(server.port, flush=True)
    time.sleep(seconds)
    server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
