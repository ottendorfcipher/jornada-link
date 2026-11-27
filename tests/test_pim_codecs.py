from datetime import date, datetime

import pytest

from jornada.cedb import CEDB_PROPDELETE, CEVT_I2, PropVal, Record
from jornada.pim import appointments, contacts, ids, tasks
from jornada.pim.models import Address, Appointment, Contact, Note, Task
from jornada.pim.notes_blob import decode_notes, encode_notes, is_ink
from jornada.pim.timeconv import (date_to_filetime, filetime_to_naive, naive_to_filetime, parse_iso, to_aware,
                                  to_wall_clock)


def test_notes_blob_round_trip_and_padding():
    assert encode_notes("a\nbc") == b"a\r\nbc\x03"
    assert encode_notes("ab") == b"ab"
    assert decode_notes(b"a\r\nbc\x03") == "a\nbc"
    assert decode_notes(b"") == "" and decode_notes(None) == ""
    assert decode_notes(encode_notes("héllo €")) == "héllo €"
    ink = b"{\\pwi" + b"\x00" * 10
    assert is_ink(ink) and decode_notes(ink) == "" and not is_ink(b"x")


def test_filetime_conversions():
    moment = datetime(2026, 9, 7, 12, 30, 15)
    assert filetime_to_naive(naive_to_filetime(moment)) == moment
    assert filetime_to_naive(0) is None
    assert filetime_to_naive(date_to_filetime(date(2026, 9, 7))) == datetime(2026, 9, 7)
    from datetime import timezone, timedelta
    aware = datetime(2026, 9, 7, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert to_wall_clock(aware, timezone.utc) == datetime(2026, 9, 7, 10, 0)
    assert to_aware(datetime(2026, 9, 7, 10), timezone.utc) == datetime(2026, 9, 7, 10, tzinfo=timezone.utc)
    assert to_wall_clock(datetime(2026, 1, 1)) == datetime(2026, 1, 1)
    assert parse_iso("2026-09-07") == date(2026, 9, 7)
    assert parse_iso("2026-09-07T10:00:00Z").tzinfo is not None


def test_appointment_round_trip_timed_and_all_day():
    timed = Appointment("Dentist", datetime(2026, 9, 7, 9, 30), datetime(2026, 9, 7, 10, 15), location="Clinic",
                        notes="bring\ncard", categories=("Health", "Personal"), busy_status="tentative",
                        private=True, reminder_minutes=15)
    props = appointments.encode(timed)
    kinds = {p.prop_id: p for p in props}
    assert kinds[ids.APPT_DURATION].value == 45 and kinds[ids.APPT_TYPE].value == ids.APPT_TYPE_NORMAL
    assert kinds[ids.REMINDER_ENABLED].value == 1 and kinds[ids.REMINDER_SOUND].value == "Alarm1.wav"
    assert kinds[ids.UNKNOWN_0002].value == 0 and kinds[ids.CATEGORIES].value == "Health,Personal"
    assert appointments.decode(Record(1, props)) == timed
    three_days = Appointment("Trip", datetime(2026, 9, 7, 8), datetime(2026, 9, 10, 0), all_day=True)
    props = appointments.encode(three_days)
    by_id = {p.prop_id: p.value for p in props}
    assert by_id[ids.APPT_TYPE] == ids.APPT_TYPE_ALL_DAY and by_id[ids.APPT_DURATION] == 2 * 1440 + 1
    assert appointments.decode(Record(2, props)) == Appointment("Trip", datetime(2026, 9, 7), datetime(2026, 9, 10), all_day=True)


@pytest.mark.parametrize("minutes,days", [(0, 1), (1, 1), (1440, 1), (1441, 2), (2880, 2), (2881, 3), (100, 1), (1500, 2)])
def test_all_day_duration_tolerates_both_conventions(minutes, days):
    assert appointments.days_from_duration(minutes) == days


def test_appointment_update_deletes_cleared_fields_and_flags_recurrence():
    existing = Record(1, appointments.encode(Appointment("A", datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10),
                                                         location="Room", notes="n", categories=("c",),
                                                         reminder_minutes=5)))
    props = appointments.encode(Appointment("A", datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10)), existing)
    deleted = {p.prop_id for p in props if p.flags & CEDB_PROPDELETE}
    assert deleted == {ids.APPT_LOCATION, ids.NOTES, ids.CATEGORIES, ids.REMINDER_MINUTES}
    recurring = Record(2, (PropVal.string(ids.SUBJECT, "Weekly"), PropVal.i2(ids.APPT_OCCURRENCE, 1)))
    decoded = appointments.decode(recurring)
    assert decoded.recurring and appointments.is_read_only(decoded) and decoded.start == datetime(1970, 1, 1)
    assert not appointments.is_read_only(Appointment("x", datetime(2026, 1, 1), datetime(2026, 1, 1)))
    missing = Record(3, (PropVal(ids.SUBJECT, 31, None, 0x100),))
    assert appointments.decode(missing).summary == ""


def test_task_round_trip_and_completion_variants():
    task = Task("Buy", due=date(2026, 1, 2), start=date(2026, 1, 1), completed=date(2026, 1, 3), priority="low",
                notes="n", categories=("x",), private=True)
    props = tasks.encode(task)
    assert tasks.decode(Record(1, props)) == task
    flag = Record(2, (PropVal.string(ids.SUBJECT, "Done"), PropVal.i2(ids.TASK_COMPLETED, 1)))
    assert tasks.decode(flag).completed == tasks.UNKNOWN_COMPLETION_DATE and tasks.decode(flag).is_completed
    open_task = Record(3, (PropVal.string(ids.SUBJECT, "Open"), PropVal.i2(ids.TASK_COMPLETED, 0),
                           PropVal.i4(ids.IMPORTANCE, ids.IMPORTANCE_HIGH)))
    assert tasks.decode(open_task).completed is None and tasks.decode(open_task).priority == "high"
    encoded = {p.prop_id: p for p in tasks.encode(Task("X", completed=tasks.UNKNOWN_COMPLETION_DATE))}
    assert encoded[ids.TASK_COMPLETED].value > 0
    cleared = tasks.encode(Task("Buy"), Record(1, props))
    assert {p.prop_id for p in cleared if p.flags & CEDB_PROPDELETE} >= {ids.TASK_DUE, ids.TASK_START, ids.TASK_COMPLETED}
    assert not tasks.is_read_only(task)


def test_contact_round_trip_slots_and_updates():
    contact = Contact(first_name="Ada", last_name="Lovelace", middle_name="King", title="Countess", company="Analytical",
                      job_title="Mathematician", emails=("a@x.org", "b@x.org", "c@x.org", "d@x.org"),
                      phones=(("work", "1"), ("work", "2"), ("work", "3"), ("home", "4"), ("mobile", "5"), ("weird", "6")),
                      addresses=(Address("home", "1 Rd", "Town", "ST", "123", "UK"), Address("work", "HQ")),
                      birthday=date(1815, 12, 10), anniversary=date(1835, 7, 8), spouse="William", notes="note",
                      categories=("Friends",), web_page="https://x.org")
    props = contacts.encode(contact)
    decoded = contacts.decode(Record(1, props))
    assert decoded.emails == ("a@x.org", "b@x.org", "c@x.org")
    assert decoded.phones == (("work", "1"), ("work2", "2"), ("home", "4"), ("mobile", "5"))
    assert decoded.full_name == "Ada King Lovelace" and decoded.addresses == contact.addresses
    assert decoded.birthday == contact.birthday and decoded.anniversary == contact.anniversary
    cleared = contacts.encode(Contact(first_name="Ada"), Record(1, props))
    deleted = {p.prop_id for p in cleared if p.flags & CEDB_PROPDELETE}
    assert ids.CONTACT_LAST_NAME in deleted and ids.CONTACT_BIRTHDAY in deleted and ids.CONTACT_NOTE in deleted
    assert contacts.encode(Contact()) == (PropVal.string(ids.CONTACT_FULL_NAME, "(unnamed)"),)
    assert not contacts.is_read_only(contact)


def test_models_normalize_and_match():
    a = Appointment(" Lunch ", datetime(2026, 9, 7, 12, 0, 5), datetime(2026, 9, 7, 13), busy_status="odd",
                    categories=(" b", "a", "b"), notes="x\r\ny")
    n = a.normalized()
    assert n.summary == "Lunch" and n.busy_status == "busy" and n.categories == ("b", "a") and n.notes == "x\ny"
    assert a.fingerprint() == n.fingerprint() and a.match_key() == "appt|lunch|2026-09-07T12:00"
    assert Appointment.from_dict(a.to_dict()) == a
    c = Contact(full_name="", first_name="Ada", last_name="Lovelace", emails=("A@x.org",))
    assert c.display_name() == "Ada Lovelace" and c.match_key() == "contact|ada lovelace|a@x.org"
    filed_last_first = Contact(full_name="Lovelace, Ada", first_name="Ada", last_name="Lovelace", emails=("a@x.org",))
    assert filed_last_first.match_key() == c.match_key()   # a device "Last, First" full name still pairs
    assert Contact(full_name="ACME Ltd").match_key() == "contact|acme ltd|"
    assert Contact(company="ACME").display_name() == "ACME" and Contact(emails=("e@x",)).display_name() == "e@x"
    assert Task("x", priority="bogus").normalized().priority == "normal"
    note = Note("T", "b\r\n", modified=datetime(2026, 1, 1))
    assert note.normalized().body == "b" and Note.from_dict(note.to_dict()) == note
    assert note.fingerprint() == Note("T", "b").fingerprint()
    assert Task.from_dict({"summary": "s", "due": "2026-01-01", "categories": ["a"], "bogus": 1}) == Task("s", due=date(2026, 1, 1), categories=("a",))


def test_note_fingerprint_ignores_folder_but_match_key_uses_title():
    device = Note("Ideas", "body")
    remote = Note("Ideas", "body", folder="Jornada", uid="x1")
    assert device.fingerprint() == remote.fingerprint()
    assert device.match_key() == remote.match_key() == "note|ideas"
    assert Note("Ideas", "other").fingerprint() != device.fingerprint()
