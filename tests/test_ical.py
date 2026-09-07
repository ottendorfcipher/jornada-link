import datetime as dt

import pytest

from jornada.webapi import ical

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    BERLIN = ZoneInfo("Europe/Berlin")
except (ImportError, ZoneInfoNotFoundError):  # pragma: no cover - depends on the host's tz database
    BERLIN = None

needs_tz = pytest.mark.skipif(BERLIN is None, reason="Europe/Berlin not available from zoneinfo")

UTC = dt.timezone.utc


def crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


GOOGLE = crlf("""BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:Work
X-WR-TIMEZONE:Europe/Berlin
BEGIN:VEVENT
DTSTART:20260310T090000Z
DTEND:20260310T100000Z
DTSTAMP:20260301T120000Z
UID:abc123@google.com
CREATED:20260201T080000Z
DESCRIPTION:Agenda:\\n1. Budget\\, Q2\\n2. Hiring
LAST-MODIFIED:20260201T080000Z
LOCATION:Room 4\\, 2nd floor
SEQUENCE:0
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
CALSCALE:GREGORIAN
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
DTSTART:19810329T020000
TZNAME:CEST
TZOFFSETTO:+0200
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
DTSTART:19961027T030000
TZNAME:CET
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
CREATED:20260201T080000Z
UID:3F2504E0-4F89-11D3-9A0C-0305E82C3301
DTEND;VALUE=DATE:20260315
TRANSP:TRANSPARENT
X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC
SUMMARY:Ski weekend
LAST-MODIFIED:20260201T080000Z
DTSTAMP:20260201T080000Z
DTSTART;VALUE=DATE:20260313
SEQUENCE:1
X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-APPLE-RADIUS=141.17;X-TITLE="Zugspitze
 \\nGarmisch-Partenkirchen, Germany":geo:47.421,10.985
LOCATION:Zugspitze\\nGarmisch-Partenkirchen\\, Germany
CATEGORIES:Sport,Friends
BEGIN:VALARM
X-WR-ALARMUID:1A5E8F3B-1
UID:1A5E8F3B-1
TRIGGER;VALUE=DATE-TIME:19760401T005545Z
ACTION:NONE
END:VALARM
BEGIN:VALARM
X-WR-ALARMUID:2B6F9A4C-2
UID:2B6F9A4C-2
TRIGGER:-PT15H
ATTACH;VALUE=URI:Chord
ACTION:AUDIO
END:VALARM
END:VEVENT
END:VCALENDAR
""")

NEXTCLOUD = crlf("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//IDN nextcloud.com//Calendar app 4.7.6//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20260201T080000Z
DTSTAMP:20260201T080000Z
LAST-MODIFIED:20260201T080000Z
SEQUENCE:2
UID:5f1c0a4e-2c3b-4a1d-9e8f-0123456789ab
DTSTART;TZID=Europe/Berlin:20260320T143000
DURATION:PT1H30M
STATUS:CONFIRMED
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


# --- parse / serialize ---------------------------------------------------------------

def test_parse_google_fixture_unescapes_text_and_keeps_structure():
    cal = ical.parse(GOOGLE)
    assert cal.name == "VCALENDAR"
    assert cal.value("PRODID") == "-//Google Inc//Google Calendar 70.9054//EN"
    event = cal.find("VEVENT")
    assert event is not None
    assert event.value("SUMMARY") == "Planning meeting"
    assert event.value("DESCRIPTION") == "Agenda:\n1. Budget, Q2\n2. Hiring"
    assert event.value("LOCATION") == "Room 4, 2nd floor"
    assert event.value("UID") == "abc123@google.com"
    assert event.value("MISSING", "dflt") == "dflt"
    assert cal.find("VALARM").value("TRIGGER") == "-P0DT0H30M0S"


def test_round_trip_is_lossless_for_every_fixture():
    for text in (GOOGLE, ICLOUD, NEXTCLOUD):
        cal = ical.parse(text)
        assert ical.parse(ical.serialize(cal)) == cal


def test_serialize_uses_crlf_and_escapes_text_but_not_structured_values():
    out = ical.serialize(ical.parse(GOOGLE))
    assert "\r\n" in out and "\n\n" not in out
    assert "DESCRIPTION:Agenda:\\n1. Budget\\, Q2\\n2. Hiring" in out
    assert "TRIGGER:-P0DT0H30M0S" in out
    rrule = ical.parse("BEGIN:VEVENT\nUID:r\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE\nEND:VEVENT")
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO,WE" in ical.serialize(rrule)


def test_quoted_parameters_with_colons_and_commas_survive():
    cal = ical.parse(ICLOUD)
    event = cal.find("VEVENT")
    location = event.get("X-APPLE-STRUCTURED-LOCATION")
    assert location.param("X-TITLE") == "Zugspitze\\nGarmisch-Partenkirchen, Germany"
    assert location.param("VALUE") == "URI"
    assert location.value == "geo:47.421,10.985"  # VALUE=URI: never escaped
    unfolded = ical.serialize(cal).replace("\r\n ", "")  # the line is longer than 75 octets
    assert 'X-TITLE="Zugspitze\\nGarmisch-Partenkirchen, Germany":geo:47.421,10.985' in unfolded
    tz = ical.parse('BEGIN:VEVENT\nUID:t\nDTSTART;TZID="America/New_York":20260101T100000\nEND:VEVENT')
    assert tz.get("DTSTART").param("TZID") == "America/New_York"
    assert "DTSTART;TZID=America/New_York:20260101T100000" in ical.serialize(tz)


def test_folding_at_75_octets_is_utf8_safe():
    summary = "Überlange Zusammenfassung mit Umlauten äöü " * 5
    cal = ical.vcalendar(ical.vevent("u", summary, dt.datetime(2026, 3, 1, 9, tzinfo=UTC), dt.datetime(2026, 3, 1, 10, tzinfo=UTC)))
    out = ical.serialize(cal)
    for physical in out.split("\r\n"):
        assert len(physical.encode("utf-8")) <= 75
    assert ical.parse(out).find("VEVENT").value("SUMMARY") == summary
    assert "\r\n " in out


def test_lf_only_input_and_missing_vcalendar_wrapper():
    event = ical.parse("BEGIN:VEVENT\nUID:x\nSUMMARY:Solo\n \x20continued\nEND:VEVENT\n")
    assert event.name == "VEVENT"
    assert event.value("SUMMARY") == "Solo continued"
    wrapped = ical.parse("BEGIN:VEVENT\nUID:x\nEND:VEVENT\nBEGIN:VTODO\nUID:y\nEND:VTODO\n")
    assert wrapped.name == "VCALENDAR"
    assert wrapped.value("VERSION") == "2.0"
    assert [c.name for c in wrapped.children] == ["VEVENT", "VTODO"]


def test_nested_components_are_preserved():
    cal = ical.parse(ICLOUD)
    assert [c.name for c in cal.children] == ["VTIMEZONE", "VEVENT"]
    assert [c.name for c in cal.find("VTIMEZONE").children] == ["DAYLIGHT", "STANDARD"]
    assert len(cal.find_all("VALARM")) == 2
    assert cal.find("STANDARD").value("TZNAME") == "CET"
    assert cal.find("NOPE") is None


@pytest.mark.parametrize("text", [
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nEND:VCALENDAR\n",
    "END:VEVENT\n",
    "BEGIN:VEVENT\nUID:x\n",
    "BEGIN:VEVENT\nthis line has no colon\nEND:VEVENT\n",
    "",
])
def test_malformed_text_raises_ical_error(text):
    with pytest.raises(ical.IcalError):
        ical.parse(text)


# --- component / property API ----------------------------------------------------------

def test_component_with_and_without_property():
    event = ical.parse(GOOGLE).find("VEVENT")
    replaced = event.with_property(ical.Property("summary", "Renamed"))
    assert replaced.value("SUMMARY") == "Renamed"
    assert len(replaced.get_all("SUMMARY")) == 1
    assert [p.name for p in replaced.properties] == [p.name for p in event.properties]
    appended = event.with_property(ical.Property("ATTENDEE", "mailto:a@example.com"), replace=False)
    assert appended.properties[-1].value == "mailto:a@example.com"
    assert event.value("SUMMARY") == "Planning meeting"  # original untouched
    assert event.without_property("SUMMARY").get("SUMMARY") is None
    assert event.with_child(ical.Component("VALARM")).children[-1].name == "VALARM"


def test_property_params_helpers():
    prop = ical.Property("tel", "+1", (("type", "WORK,VOICE"), ("TYPE", "pref")))
    assert prop.name == "TEL"
    assert prop.param("TYPE") == "WORK,VOICE"
    assert prop.param_values("type") == ("WORK", "VOICE", "pref")
    assert prop.param("nope") is None
    assert prop.with_params({"VALUE": "TEXT"}).params == (("VALUE", "TEXT"),)
    with pytest.raises(ical.IcalError):
        ical.Property("X", 5)  # type: ignore[arg-type]
    with pytest.raises(ical.IcalError):
        ical.Property("X", "v", ("not-a-pair",))  # type: ignore[arg-type]
    assert ical.Property("X", "v", {"a": "1"}).params == (("A", "1"),)


def test_parse_tolerates_empty_parameters_but_not_missing_names():
    prop = ical.parse("BEGIN:VEVENT\nSUMMARY;;X-A=1;:x\nEND:VEVENT").get("SUMMARY")
    assert prop.params == (("X-A", "1"),)
    with pytest.raises(ical.IcalError):
        ical.parse("BEGIN:VEVENT\n:no name\nEND:VEVENT")
    with pytest.raises(ical.IcalError):
        ical.parse_datetime_value("2026-01-01", {"VALUE": "DATE"})
    naive_stamp = ical.vevent("u", "s", dt.datetime(2026, 1, 1, 9), None, dtstamp=dt.datetime(2026, 1, 1, 8))
    assert naive_stamp.value("DTSTAMP") == "20260101T080000Z"


# --- dates, times, durations -------------------------------------------------------------

def test_parse_datetime_variants():
    assert ical.parse_datetime(ical.Property("DTSTART", "20260313", (("VALUE", "DATE"),))) == dt.date(2026, 3, 13)
    assert ical.parse_datetime_value("20260313") == dt.date(2026, 3, 13)
    floating = ical.parse_datetime_value("20260313T143000")
    assert floating == dt.datetime(2026, 3, 13, 14, 30) and floating.tzinfo is None
    utc = ical.parse_datetime_value("20260313T143000Z")
    assert utc == dt.datetime(2026, 3, 13, 14, 30, tzinfo=UTC)
    unknown = ical.parse_datetime_value("20260313T143000", {"TZID": "Mars/Olympus_Mons"})
    assert unknown.tzinfo is None
    assert ical.parse_datetime_value("20260313T1430") == dt.datetime(2026, 3, 13, 14, 30)


@needs_tz
def test_parse_datetime_with_tzid_uses_zoneinfo():
    moment = ical.parse_datetime_value("20260320T143000", (("TZID", "Europe/Berlin"),))
    assert moment.utcoffset() == dt.timedelta(hours=1)
    assert moment.astimezone(UTC) == dt.datetime(2026, 3, 20, 13, 30, tzinfo=UTC)
    prefixed = ical.parse_datetime_value("20260720T090000", {"TZID": "/mozilla.org/20050126_1/Europe/Berlin"})
    assert prefixed.utcoffset() == dt.timedelta(hours=2)
    assert ical.resolve_zone("UTC") is UTC


@pytest.mark.parametrize("value", ["2026031", "20260313T", "20261313", "20260313T250000", "yesterday"])
def test_parse_datetime_rejects_garbage(value):
    with pytest.raises(ical.IcalError):
        ical.parse_datetime_value(value)


def test_format_and_datetime_property():
    assert ical.format_date(dt.date(2026, 3, 13)) == "20260313"
    assert ical.format_datetime(dt.datetime(2026, 3, 13, 14, 30)) == "20260313T143000"
    aware = dt.datetime(2026, 3, 13, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    assert ical.format_datetime(aware) == "20260313T143000Z"
    date_prop = ical.datetime_property("DTSTART", dt.date(2026, 3, 13))
    assert (date_prop.value, date_prop.params) == ("20260313", (("VALUE", "DATE"),))
    assert ical.datetime_property("DTEND", aware).value == "20260313T143000Z"
    with pytest.raises(ical.IcalError):
        ical.datetime_property("DTSTART", "20260313")  # type: ignore[arg-type]


@needs_tz
def test_datetime_property_with_tzid_writes_local_wall_clock():
    utc_moment = dt.datetime(2026, 3, 20, 13, 30, tzinfo=UTC)
    prop = ical.datetime_property("DTSTART", utc_moment, tzid="Europe/Berlin")
    assert prop.value == "20260320T143000"
    assert prop.param("TZID") == "Europe/Berlin"
    assert ical.parse_datetime(prop) == utc_moment
    naive = ical.datetime_property("DTSTART", dt.datetime(2026, 3, 20, 14, 30), tzid="Europe/Berlin")
    assert naive.value == "20260320T143000"


@pytest.mark.parametrize("text,expected", [
    ("PT15M", dt.timedelta(minutes=15)),
    ("-PT15M", dt.timedelta(minutes=-15)),
    ("+P1W", dt.timedelta(weeks=1)),
    ("P1DT2H3M4S", dt.timedelta(days=1, hours=2, minutes=3, seconds=4)),
    ("-P0DT0H30M0S", dt.timedelta(minutes=-30)),
    ("PT0S", dt.timedelta(0)),
])
def test_parse_duration(text, expected):
    assert ical.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["P", "PT", "15M", "P1X", "-P1WT", "PT1H2X", ""])
def test_parse_duration_rejects_malformed(text):
    with pytest.raises(ical.IcalError):
        ical.parse_duration(text)


@pytest.mark.parametrize("delta,expected", [
    (dt.timedelta(minutes=15), "PT15M"),
    (dt.timedelta(minutes=-15), "-PT15M"),
    (dt.timedelta(minutes=-90), "-PT1H30M"),
    (dt.timedelta(weeks=2), "P2W"),
    (dt.timedelta(days=8), "P8D"),
    (dt.timedelta(days=1, seconds=5), "P1DT5S"),
    (dt.timedelta(0), "PT0S"),
])
def test_format_duration_round_trips(delta, expected):
    assert ical.format_duration(delta) == expected
    assert ical.parse_duration(expected) == delta


# --- builders ------------------------------------------------------------------------------

def test_vevent_all_day_uses_value_date_with_exclusive_end():
    single = ical.vevent("uid-1", "Holiday", dt.date(2026, 5, 1), dt.date(2026, 5, 1), all_day=True)
    assert single.get("DTSTART").param("VALUE") == "DATE"
    assert single.value("DTSTART") == "20260501"
    assert single.value("DTEND") == "20260502"
    assert single.get("DTEND").param("VALUE") == "DATE"
    two_days = ical.vevent("uid-2", "Trip", dt.date(2026, 5, 1), dt.date(2026, 5, 3), all_day=True)
    assert ical.event_span(two_days) == (dt.date(2026, 5, 1), dt.date(2026, 5, 3))
    # a device end time inside the last day rounds up to the exclusive date
    from_device = ical.vevent("uid-3", "Trip", dt.datetime(2026, 5, 1), dt.datetime(2026, 5, 2, 23, 59), all_day=True)
    assert from_device.value("DTEND") == "20260503"
    assert ical.vevent("uid-4", "Open", dt.date(2026, 5, 1), None, all_day=True).value("DTEND") == "20260502"
    assert ical.is_all_day(single)


def test_vevent_timed_with_all_options_is_parseable():
    start = dt.datetime(2026, 6, 1, 9, tzinfo=UTC)
    event = ical.vevent(
        "uid-9", "Call: a, b; c", start, start + dt.timedelta(hours=1),
        location="Booth 2", description="Line 1\nLine 2", categories=("Work", "A,B"),
        alarm_minutes_before=15, transparent=True, dtstamp=dt.datetime(2026, 1, 1, tzinfo=UTC),
        extra=(ical.Property("X-JORNADA-OID", "42"),),
    )
    text = ical.serialize(ical.vcalendar(event, method="PUBLISH"))
    assert "SUMMARY:Call: a\\, b\\; c" in text
    assert "CATEGORIES:Work,A\\,B" in text
    assert "TRIGGER:-PT15M" in text and "ACTION:DISPLAY" in text
    assert "TRANSP:TRANSPARENT" in text and "METHOD:PUBLISH" in text
    assert "DTSTAMP:20260101T000000Z" in text
    back = ical.parse(text)
    ev = back.find("VEVENT")
    assert ev.value("SUMMARY") == "Call: a, b; c"
    assert ev.value("DESCRIPTION") == "Line 1\nLine 2"
    assert ical.categories_of(ev) == ("Work", "A,B")
    assert ical.alarm_minutes_before(ev) == 15
    assert ical.event_span(ev) == (start, start + dt.timedelta(hours=1))
    assert ev.value("X-JORNADA-OID") == "42"
    assert ical.vevent("u", "s", start, start, transparent=False).value("TRANSP") == "OPAQUE"
    assert ical.vevent("u", "s", start, None).get("DTEND") is None


def test_vevent_validation_errors():
    start = dt.datetime(2026, 6, 1, 9)
    with pytest.raises(ical.IcalError):
        ical.vevent("", "s", start, start)
    with pytest.raises(ical.IcalError):
        ical.vevent("u", "s", start, start, alarm_minutes_before=-5)
    with pytest.raises(ical.IcalError):
        ical.vevent("u", "s", "20260601", "20260601", all_day=True)  # type: ignore[arg-type]


def test_vtodo_builder():
    todo = ical.vtodo(
        "todo-1", "Buy milk", due=dt.date(2026, 6, 2), start=dt.datetime(2026, 6, 1, 8, tzinfo=UTC),
        completed=dt.datetime(2026, 6, 2, 9, tzinfo=UTC), priority=1, description="2%",
        categories=("Errands",), status="completed", percent_complete=100,
        dtstamp=dt.datetime(2026, 1, 1, tzinfo=UTC),
    )
    text = ical.serialize(ical.vcalendar(todo))
    assert "DUE;VALUE=DATE:20260602" in text
    assert "DTSTART:20260601T080000Z" in text
    assert "COMPLETED:20260602T090000Z" in text
    assert "PRIORITY:1" in text and "STATUS:COMPLETED" in text and "PERCENT-COMPLETE:100" in text
    back = ical.parse(text).find("VTODO")
    assert back.value("SUMMARY") == "Buy milk"
    assert ical.parse_datetime(back.get("DUE")) == dt.date(2026, 6, 2)
    with pytest.raises(ical.IcalError):
        ical.vtodo("t", "s", priority=12)
    with pytest.raises(ical.IcalError):
        ical.vtodo("t", "s", percent_complete=101)


# --- readers -------------------------------------------------------------------------------

def test_alarm_minutes_before_on_fixtures():
    assert ical.alarm_minutes_before(ical.parse(GOOGLE).find("VEVENT")) == 30
    assert ical.alarm_minutes_before(ical.parse(ICLOUD).find("VEVENT")) == 900  # ACTION:NONE placeholder skipped
    assert ical.alarm_minutes_before(ical.parse(NEXTCLOUD).find("VEVENT")) == 60
    no_alarm = ical.parse("BEGIN:VEVENT\nUID:x\nEND:VEVENT")
    assert ical.alarm_minutes_before(no_alarm) is None
    positive = ical.parse("BEGIN:VEVENT\nUID:x\nBEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:PT5M\nEND:VALARM\nEND:VEVENT")
    assert ical.alarm_minutes_before(positive) == 0
    absolute = ical.parse("BEGIN:VEVENT\nUID:x\nBEGIN:VALARM\nACTION:DISPLAY\nTRIGGER;VALUE=DATE-TIME:20260101T000000Z\nEND:VALARM\nEND:VEVENT")
    assert ical.alarm_minutes_before(absolute) is None
    broken = ical.parse("BEGIN:VEVENT\nUID:x\nBEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:soon\nEND:VALARM\nEND:VEVENT")
    assert ical.alarm_minutes_before(broken) is None


def test_categories_of_and_is_all_day():
    assert ical.categories_of(ical.parse(ICLOUD).find("VEVENT")) == ("Sport", "Friends")
    assert ical.categories_of(ical.parse(NEXTCLOUD).find("VEVENT")) == ("Health,Personal", "Family")
    assert ical.categories_of(ical.parse(GOOGLE).find("VEVENT")) == ()
    multi = ical.parse("BEGIN:VEVENT\nUID:x\nCATEGORIES: A , ,B\nCATEGORIES:C\nEND:VEVENT")
    assert ical.categories_of(multi) == ("A", "B", "C")
    assert ical.is_all_day(ical.parse(ICLOUD).find("VEVENT"))
    assert not ical.is_all_day(ical.parse(GOOGLE).find("VEVENT"))
    assert not ical.is_all_day(ical.parse("BEGIN:VEVENT\nUID:x\nEND:VEVENT"))
    assert ical.is_all_day(ical.parse("BEGIN:VEVENT\nUID:x\nDTSTART:20260101\nEND:VEVENT"))


def test_event_span_variants():
    google = ical.event_span(ical.parse(GOOGLE).find("VEVENT"))
    assert google == (dt.datetime(2026, 3, 10, 9, tzinfo=UTC), dt.datetime(2026, 3, 10, 10, tzinfo=UTC))
    assert ical.event_span(ical.parse(ICLOUD).find("VEVENT")) == (dt.date(2026, 3, 13), dt.date(2026, 3, 15))
    start, end = ical.event_span(ical.parse(NEXTCLOUD).find("VEVENT"))
    assert end - start == dt.timedelta(minutes=90)
    assert (start.hour, start.minute) == (14, 30)
    if BERLIN is not None:
        assert start.utcoffset() == dt.timedelta(hours=1)
    open_ended = ical.parse("BEGIN:VEVENT\nUID:x\nDTSTART:20260101T100000Z\nEND:VEVENT")
    assert ical.event_span(open_ended) == (dt.datetime(2026, 1, 1, 10, tzinfo=UTC),) * 2
    all_day = ical.parse("BEGIN:VEVENT\nUID:x\nDTSTART;VALUE=DATE:20260101\nEND:VEVENT")
    assert ical.event_span(all_day) == (dt.date(2026, 1, 1), dt.date(2026, 1, 2))
    with pytest.raises(ical.IcalError):
        ical.event_span(ical.parse("BEGIN:VEVENT\nUID:x\nEND:VEVENT"))
