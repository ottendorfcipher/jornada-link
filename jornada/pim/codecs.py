"""The device codecs for the three Pocket Outlook databases."""
from __future__ import annotations

from typing import Optional

from . import appointments, contacts, ids, tasks
from .store import Codec

APPOINTMENTS = Codec(ids.DB_APPOINTMENTS, appointments.decode, appointments.encode, appointments.is_read_only)
CONTACTS = Codec(ids.DB_CONTACTS, contacts.decode, contacts.encode, contacts.is_read_only)
TASKS = Codec(ids.DB_TASKS, tasks.decode, tasks.encode, tasks.is_read_only)

ALL = (APPOINTMENTS, CONTACTS, TASKS)


def codec_for(database_name: str) -> Optional[Codec]:
    wanted = database_name.casefold()
    return next((c for c in ALL if c.database.casefold() == wanted), None)


def generic(database_name: str) -> Codec:
    """A pass-through codec for databases the tool does not model (snapshots, dumps)."""
    return Codec(database_name, lambda record: record, lambda record, _existing: record.props)
