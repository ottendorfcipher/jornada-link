import datetime as dt
from dataclasses import replace

import pytest

from jornada.pim.models import Appointment
from jornada.sync.calendar import icalmap
from jornada.sync.calendar.common import local_wall_clock
from jornada.webapi import ical

UTC = dt.timezone.utc


def crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


GOOGLE = crlf("""BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260310T090000Z
DTEND:20260310T100000Z
DTSTAMP:20260301T120000Z
UID:abc123@google.com
DESCRIPTION:Agenda:\\n1. Budget\\, Q2\\n2. Hiring
LOCATION:Room 4\\, 2nd floor
STATUS:CONFIRMED
SUMMARY:Planning meeting
TRANSP:OPAQUE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:This is an event reminder
TRIGGER:-P0DT0H30M0S
END:VALARM
END:VEVENT
END:VCALENDAR
""")

ICLOUD = crlf("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Apple Inc.//macOS 14.5//EN
BEGIN:VEVENT
UID:3F2504E0-4F89-11D3-9A0C-0305E82C3301
DTEND;VALUE=DATE:20260315
TRANSP:TRANSPARENT
SUMMARY:Ski weekend
DTSTAMP:20260201T080000Z
DTSTART;VALUE=DATE:20260313
LOCATION:Zugspitze\\nGarmisch-Partenkirchen\\, Germany
CATEGORIES:Sport,Friends
BEGIN:VALARM
TRIGGER;VALUE=DATE-TIME:19760401T005545Z
ACTION:NONE
END:VALARM
BEGIN:VALARM
TRIGGER:-PT15H
ACTION:AUDIO
END:VALARM
END:VEVENT
END:VCALENDAR
""")

NEXTCLOUD = crlf("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//IDN nextcloud.com//Calendar app 4.7.6//EN
BEGIN:VEVENT
DTSTAMP:20260201T080000Z
UID:5f1c0a4e-2c3b-4a1d-9e8f-0123456789ab
DTSTART;TZID=Europe/Berlin:20260320T143000
DURATION:PT1H30M
SUMMARY:Dentist
CLASS:PRIVATE
CATEGORIES:Health\\,Personal,Family
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER;RELATED=START:-PT1H
DESCRIPTION:Dentist
END:VALARM
END:VEVENT
END:VCALENDAR
""")


def event(*lines: str) -> ical.Component:
    return ical.parse("BEGIN:VEVENT\nUID:x\n" + "\n".join(lines) + "\nEND:VEVENT")


def round_trip(appointment: Appointment, uid: str = "uid-1"):
    stamp = dt.datetime(2026, 1, 1, tzinfo=UTC)
    text = ical.serialize(ical.vcalendar(icalmap.to_vevent(appointment, uid, dtstamp=stamp)))
    return text, icalmap.from_vevent(ical.parse(text).find("VEVENT"))


# -- writing ------------------------------------------------------------------------
def test_timed_round_trip_keeps_every_field():
    appt = Appointment("Dentist", dt.datetime(2026, 9, 7, 9, 30), dt.datetime(2026, 9, 7, 10, 15), location="Clinic",
                       notes="bring card\nsecond line", categories=("Health", "A,B"), busy_status="tentative",
                       private=True, reminder_minutes=15)
    text, back = round_trip(appt)
    assert back == replace(appt, uid="uid-1")
    assert "X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE" in text and "TRANSP:OPAQUE" in text and "CLASS:PRIVATE" in text
    assert "TRIGGER:-PT15M" in text and "CATEGORIES:Health,A\\,B" in text and "DTSTAMP:20260101T000000Z" in text
    assert "DESCRIPTION:bring card\\nsecond line" in text and "LOCATION:Clinic" in text


def test_timed_values_are_written_as_utc_instants_of_the_local_wall_clock():
    vevent = icalmap.to_vevent(Appointment("A", dt.datetime(2026, 1, 15, 9), dt.datetime(2026, 1, 15, 8)), "u")
    start = ical.parse_datetime(vevent.get("DTSTART"))
    assert start.tzinfo is UTC and local_wall_clock(start) == dt.datetime(2026, 1, 15, 9)
    assert vevent.value("DTSTART").endswith("Z") and vevent.get("DTSTART").param("TZID") is None
    assert vevent.value("DTEND") == vevent.value("DTSTART")     # an end before the start is clamped


def test_all_day_round_trip_uses_value_date_with_exclusive_end():
    trip = Appointment("Trip", dt.datetime(2026, 9, 7), dt.datetime(2026, 9, 10), all_day=True, busy_status="free")
    text, back = round_trip(trip, "uid-2")
    assert "DTSTART;VALUE=DATE:20260907" in text and "DTEND;VALUE=DATE:20260910" in text
    assert "TRANSP:TRANSPARENT" in text and "X-MICROSOFT-CDO-BUSYSTATUS:FREE" in text and "CLASS" not in text
    assert back == replace(trip, uid="uid-2")
    single = icalmap.to_vevent(Appointment("Day", dt.datetime(2026, 9, 7), dt.datetime(2026, 9, 7), all_day=True), "u")
    assert (single.value("DTSTART"), single.value("DTEND")) == ("20260907", "20260908")
    inside = Appointment("Day", dt.datetime(2026, 9, 7, 8), dt.datetime(2026, 9, 8, 23, 59), all_day=True)
    assert icalmap.to_vevent(inside, "u").value("DTEND") == "20260909"


@pytest.mark.parametrize("busy,transp,outlook", [
    ("free", "TRANSPARENT", "FREE"), ("tentative", "OPAQUE", "TENTATIVE"),
    ("busy", "OPAQUE", "BUSY"), ("out_of_office", "OPAQUE", "OOF"),
])
def test_busy_status_round_trips(busy, transp, outlook):
    appt = Appointment("A", dt.datetime(2026, 9, 7, 9), dt.datetime(2026, 9, 7, 10), busy_status=busy)
    vevent = icalmap.to_vevent(appt, "u")
    assert (vevent.value("TRANSP"), vevent.value(icalmap.BUSY_STATUS_PROPERTY)) == (transp, outlook)
    assert icalmap.from_vevent(vevent).busy_status == busy


def test_reminder_private_categories_and_validation():
    appt = Appointment("A", dt.datetime(2026, 9, 7, 9), dt.datetime(2026, 9, 7, 10), reminder_minutes=0, private=True,
                       categories=("One", " ", "Two", "One"))
    vevent = icalmap.to_vevent(appt, "u")
    assert ical.alarm_minutes_before(vevent) == 0 and ical.categories_of(vevent) == ("One", "Two")
    back = icalmap.from_vevent(vevent)
    assert back.reminder_minutes == 0 and back.private and back.categories == ("One", "Two")
    plain = icalmap.to_vevent(replace(appt, reminder_minutes=None, private=False), "u")
    assert plain.find("VALARM") is None and plain.get("CLASS") is None
    assert icalmap.from_vevent(plain).reminder_minutes is None and not icalmap.from_vevent(plain).private
    assert ical.alarm_minutes_before(icalmap.to_vevent(replace(appt, reminder_minutes=-5), "u")) == 0
    with pytest.raises(ical.IcalError):
        icalmap.to_vevent(appt, "")


# -- reading ------------------------------------------------------------------------
def test_google_style_fixture():
    appt = icalmap.from_vevent(ical.parse(GOOGLE).find("VEVENT"))
    assert appt.summary == "Planning meeting" and appt.uid == "abc123@google.com"
    assert appt.start == local_wall_clock(dt.datetime(2026, 3, 10, 9, tzinfo=UTC))
    assert appt.end - appt.start == dt.timedelta(hours=1) and not appt.all_day
    assert appt.location == "Room 4, 2nd floor" and appt.notes == "Agenda:\n1. Budget, Q2\n2. Hiring"
    assert appt.reminder_minutes == 30 and appt.busy_status == "busy" and not appt.private and not appt.recurring


def test_icloud_style_all_day_fixture():
    appt = icalmap.from_vevent(ical.parse(ICLOUD).find("VEVENT"))
    assert appt.all_day and (appt.start, appt.end) == (dt.datetime(2026, 3, 13), dt.datetime(2026, 3, 15))
    assert appt.busy_status == "free" and appt.categories == ("Sport", "Friends") and appt.reminder_minutes == 900
    assert appt.location == "Zugspitze\nGarmisch-Partenkirchen, Germany" and appt.summary == "Ski weekend"


def test_nextcloud_style_tzid_fixture():
    appt = icalmap.from_vevent(ical.parse(NEXTCLOUD).find("VEVENT"))
    expected = local_wall_clock(ical.parse_datetime_value("20260320T143000", {"TZID": "Europe/Berlin"}))
    assert appt.start == expected and appt.end == expected + dt.timedelta(minutes=90)
    assert appt.private and appt.reminder_minutes == 60 and appt.categories == ("Health,Personal", "Family")
    assert appt.busy_status == "busy" and not appt.all_day


def test_floating_times_missing_dtend_and_date_values():
    floating = icalmap.from_vevent(event("SUMMARY:Float", "DTSTART:20260320T143000"))
    assert floating.start == dt.datetime(2026, 3, 20, 14, 30) and floating.end == floating.start and not floating.all_day
    assert icalmap.from_vevent(event("DTSTART:20260320T143000", "DURATION:PT45M")).end == dt.datetime(2026, 3, 20, 15, 15)
    day = icalmap.from_vevent(event("DTSTART;VALUE=DATE:20260320"))
    assert day.all_day and (day.start, day.end) == (dt.datetime(2026, 3, 20), dt.datetime(2026, 3, 21))
    bare = icalmap.from_vevent(event("DTSTART:20260320", "DTEND:20260320"))
    assert bare.all_day and bare.end == dt.datetime(2026, 3, 21)
    late_end = icalmap.from_vevent(event("DTSTART;VALUE=DATE:20260320", "DTEND:20260321T235900Z"))
    assert late_end.end >= dt.datetime(2026, 3, 22)
    backwards = icalmap.from_vevent(event("DTSTART:20260320T140000Z", "DTEND:20260320T130000Z"))
    assert backwards.end == backwards.start
    empty = icalmap.from_vevent(event("DTSTART:20260320T140000Z"))
    assert empty.summary == "" and empty.location == "" and empty.notes == "" and empty.categories == ()
    with pytest.raises(ical.IcalError):
        icalmap.from_vevent(event("SUMMARY:no start"))


def test_busy_status_reading_rules():
    def status(*lines):
        return icalmap.busy_status_of(event("DTSTART:20260101T100000Z", *lines))

    assert status("TRANSP:TRANSPARENT") == "free" and status("TRANSP:OPAQUE") == "busy" and status() == "busy"
    assert status("X-MICROSOFT-CDO-BUSYSTATUS:FREE") == "free"                    # Outlook without TRANSP
    assert status("TRANSP:OPAQUE", "X-MICROSOFT-CDO-BUSYSTATUS:FREE") == "busy"   # TRANSP decides free vs not
    assert status("TRANSP:TRANSPARENT", "X-MICROSOFT-CDO-BUSYSTATUS:BUSY") == "free"
    assert status("X-MICROSOFT-CDO-BUSYSTATUS:OOF") == "out_of_office"
    assert status("X-MICROSOFT-CDO-BUSYSTATUS:WORKINGELSEWHERE") == "busy"
    assert status("TRANSP:OPAQUE", "X-MICROSOFT-CDO-BUSYSTATUS:tentative") == "tentative"
    assert icalmap.is_private(event("CLASS:CONFIDENTIAL")) and not icalmap.is_private(event("CLASS:PUBLIC"))


def test_recurring_detection_and_master_lookup():
    assert icalmap.from_vevent(event("DTSTART:20260101T100000Z", "RRULE:FREQ=WEEKLY")).recurring
    assert icalmap.is_recurring(event("DTSTART:20260101T100000Z", "RDATE:20260108T100000Z"))
    override = event("DTSTART:20260108T110000Z", "RECURRENCE-ID:20260108T100000Z")
    assert icalmap.is_recurring(override) and icalmap.master_vevent(override) is None
    google = ical.parse(GOOGLE)
    assert icalmap.master_vevent(google).value("SUMMARY") == "Planning meeting"
    assert not icalmap.from_vevent(google.find("VEVENT")).recurring
    both = ical.parse("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:r\nDTSTART:20260108T110000Z\nRECURRENCE-ID:20260108T100000Z\n"
                      "END:VEVENT\nBEGIN:VEVENT\nUID:r\nDTSTART:20260101T100000Z\nRRULE:FREQ=WEEKLY\nEND:VEVENT\nEND:VCALENDAR")
    master = icalmap.master_vevent(both)
    assert master is not None and master.get("RRULE") is not None and icalmap.from_vevent(master).recurring
    assert icalmap.master_vevent(ical.parse("BEGIN:VTODO\nUID:t\nEND:VTODO")) is None
