"""Backend "apple": one calendar in Calendar.app ⇄ the device's appointments, through JXA.

Every operation is one JavaScript-for-Automation script run by ``osascript``.
The scripts are constants: calendar names, summaries, notes, dates and ids
always travel as ``argv`` entries and are never interpolated into script
source. Calendar.app's scripting dictionary has no busy status, privacy or
categories, so events read from it count as busy, public and uncategorised
and those device fields do not travel to Calendar.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Set, Tuple

from ...pim.models import Appointment
from ...pim.timeconv import midnight, parse_iso
from ...webapi.applescript import AppleScriptError, Runner, run_jxa
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import Log, all_day_end, iso_offset, local_wall_clock, text_of
from .window import resolve_window, windowed

DEFAULT_CALENDAR = "Jornada"
CALENDAR_SETTING = "calendar"
_ONE_SECOND = timedelta(seconds=1)

_PRELUDE = """
function calendarApp() { return Application("Calendar"); }
function calendarNamed(Calendar, name) {
  const existing = Calendar.calendars.whose({name: name});
  if (existing.length > 0) { return existing[0]; }
  Calendar.calendars.push(Calendar.Calendar({name: name}));
  const created = Calendar.calendars.whose({name: name});
  if (created.length === 0) { throw new Error("Calendar did not create the calendar " + name); }
  return created[0];
}
function eventWithUid(cal, uid) {
  const found = cal.events.whose({uid: uid});
  return found.length > 0 ? found[0] : null;
}
function stamp(date) { return date ? new Date(date).toISOString() : ""; }
function text(value) { return value ? String(value) : ""; }
function firstAlarm(ev) {
  const alarms = ev.displayAlarms();
  return alarms.length > 0 ? alarms[0].triggerInterval() : null;
}
function describe(ev) {
  return {uid: ev.uid(), summary: text(ev.summary()), startDate: stamp(ev.startDate()),
          endDate: stamp(ev.endDate()), alldayEvent: Boolean(ev.alldayEvent()), location: text(ev.location()),
          description: text(ev.description()), recurrence: text(ev.recurrence()),
          stamp: stamp(ev.stampDate()), alarmMinutes: firstAlarm(ev)};
}
function eventProperties(argv, i) {
  return {summary: argv[i], startDate: new Date(argv[i + 1]), endDate: new Date(argv[i + 2]),
          alldayEvent: argv[i + 3] === "1", location: argv[i + 4], description: argv[i + 5]};
}
function setAlarm(Calendar, ev, minutes) {
  ev.displayAlarms().forEach(function (alarm) { alarm.delete(); });
  if (minutes !== "") { ev.displayAlarms.push(Calendar.DisplayAlarm({triggerInterval: -Number(minutes)})); }
}
"""

LIST_SCRIPT = _PRELUDE + """
function run(argv) {
  const Calendar = calendarApp();
  const cal = calendarNamed(Calendar, argv[0]);
  const start = new Date(argv[1]), end = new Date(argv[2]);
  const events = cal.events.whose({_and: [{startDate: {_lessThan: end}}, {endDate: {_greaterThan: start}}]})();
  return JSON.stringify(events.map(describe));
}
"""

CREATE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Calendar = calendarApp();
  const cal = calendarNamed(Calendar, argv[0]);
  const ev = Calendar.Event(eventProperties(argv, 1));
  cal.events.push(ev);
  setAlarm(Calendar, ev, argv[7]);
  return JSON.stringify(describe(ev));
}
"""

UPDATE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Calendar = calendarApp();
  const ev = eventWithUid(calendarNamed(Calendar, argv[0]), argv[1]);
  if (ev === null) { throw new Error("the event is no longer in Calendar"); }
  const props = eventProperties(argv, 2);
  ev.alldayEvent = props.alldayEvent;
  ev.summary = props.summary;
  ev.startDate = props.startDate;
  ev.endDate = props.endDate;
  ev.location = props.location;
  ev.description = props.description;
  setAlarm(Calendar, ev, argv[8]);
  return JSON.stringify(describe(ev));
}
"""

DELETE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Calendar = calendarApp();
  const ev = eventWithUid(calendarNamed(Calendar, argv[0]), argv[1]);
  const existed = ev !== null;
  if (existed) { ev.delete(); }
  return JSON.stringify({uid: argv[1], deleted: existed});
}
"""


# -- pure mapping --------------------------------------------------------------
def event_arguments(record: Appointment) -> Tuple[str, ...]:
    """argv entries for the create/update scripts: summary, start, end, all-day flag, location, notes, alarm.

    All-day events are sent as local midnight to 23:59:59 of their last day, which Calendar.app
    accepts whether it treats the end as inclusive or exclusive.
    """
    appt = record.normalized()
    if appt.all_day:
        start, end = midnight(appt.start), all_day_end(appt.start, appt.end) - _ONE_SECOND
    else:
        start, end = appt.start, max(appt.end, appt.start)
    reminder = "" if appt.reminder_minutes is None else str(max(int(appt.reminder_minutes), 0))
    return (appt.summary, iso_offset(start), iso_offset(end), "1" if appt.all_day else "0",
            appt.location, appt.notes, reminder)


def event_record(entry: Dict[str, Any]) -> Appointment:
    """The Appointment described by one JSON entry of the scripts (ValueError when unreadable)."""
    all_day = bool(entry.get("alldayEvent"))
    start, end = event_span(_moment(entry.get("startDate")), _moment(entry.get("endDate")), all_day)
    return Appointment(summary=text_of(entry.get("summary")), start=start, end=end, all_day=all_day,
                       location=text_of(entry.get("location")), notes=text_of(entry.get("description")),
                       reminder_minutes=reminder_from_trigger(entry.get("alarmMinutes")),
                       recurring=bool(text_of(entry.get("recurrence")).strip()), uid=text_of(entry.get("uid")))


def event_span(start: datetime, end: datetime, all_day: bool) -> Tuple[datetime, datetime]:
    """Calendar's dates → naive local span; all-day events snap to midnight with an exclusive end."""
    if all_day:
        first = midnight(start)
        return first, all_day_end(first, end)
    return start, max(end, start)


def _moment(text: Any) -> datetime:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("missing date")
    moment = parse_iso(text)
    return local_wall_clock(moment if isinstance(moment, datetime) else midnight(moment))


def reminder_from_trigger(interval: Any) -> Optional[int]:
    """Calendar's display-alarm trigger interval (minutes; negative = before) → minutes before start."""
    if interval is None or isinstance(interval, bool):
        return None
    try:
        minutes = int(interval)
    except (TypeError, ValueError):
        return None
    return max(-minutes, 0)


def _uid_of(answer: Any, what: str) -> str:
    uid = answer.get("uid") if isinstance(answer, dict) else None
    if not isinstance(uid, str) or not uid:
        raise StoreError(f"Calendar did not report the uid of the event it should {what}")
    return uid


def _check_uid(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip():
        raise StoreError("Calendar needs an event uid to update or delete")


# -- the store -----------------------------------------------------------------------
class AppleCalendarStore:
    """Store protocol over the events of one Calendar.app calendar inside the window."""

    name = "apple-calendar"

    def __init__(self, start: datetime, end: datetime, calendar: str = DEFAULT_CALENDAR,
                 runner: Optional[Runner] = None, log: Log = lambda _line: None) -> None:
        self._calendar = calendar.strip() or DEFAULT_CALENDAR
        self._start = start
        self._end = end
        self._runner = runner
        self._log = log
        self._recurring: Set[str] = set()

    @property
    def calendar(self) -> str:
        return self._calendar

    def _run(self, script: str, args: Tuple[str, ...], what: str) -> Any:
        try:
            return run_jxa(script, args, runner=self._runner)
        except AppleScriptError as exc:
            raise StoreError(f"Calendar could not {what}: {exc}") from exc

    # -- Store protocol ---------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        args = (self._calendar, iso_offset(self._start), iso_offset(self._end))
        answer = self._run(LIST_SCRIPT, args, f"list the calendar {self._calendar!r}")
        if not isinstance(answer, list):
            raise StoreError("Calendar returned an unexpected listing")
        return tuple(self._item(entry) for entry in answer)

    def _item(self, entry: Any) -> Item:
        uid = _uid_of(entry, "list")
        version = text_of(entry.get("stamp")) or None
        try:
            record = event_record(entry)
        except (ValueError, OverflowError) as exc:
            self._log(f"Calendar event {uid} cannot be read ({exc}); it is left alone")
            return Item(id=uid, record=None, version=version, problem=str(exc))
        self._remember(uid, record.recurring)
        return Item(id=uid, record=record, version=version, read_only=record.recurring)

    def _remember(self, uid: str, recurring: bool) -> None:
        if recurring:
            self._recurring.add(uid)
        else:
            self._recurring.discard(uid)

    def create(self, record: Appointment) -> str:
        args = (self._calendar,) + event_arguments(record)
        answer = self._run(CREATE_SCRIPT, args, f"create the event {record.summary!r}")
        return _uid_of(answer, "create")

    def update(self, item_id: str, record: Appointment) -> Optional[str]:
        _check_uid(item_id)
        if item_id in self._recurring:
            raise StoreError(f"{record.summary!r} is a recurring series in Calendar; it is left unchanged "
                             "(change it in Calendar)")
        args = (self._calendar, item_id) + event_arguments(record)
        answer = self._run(UPDATE_SCRIPT, args, f"update the event {record.summary!r}")
        return text_of(answer.get("stamp")) or None if isinstance(answer, dict) else None

    def delete(self, item_id: str) -> None:
        _check_uid(item_id)
        answer = self._run(DELETE_SCRIPT, (self._calendar, item_id), "delete the event")
        if isinstance(answer, dict) and answer.get("deleted") is False:
            self._log(f"event {item_id} was already gone from Calendar")
        self._recurring.discard(item_id)


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Any:
    start, end = resolve_window(account, context)
    store = AppleCalendarStore(start, end, calendar=account.setting(CALENDAR_SETTING) or DEFAULT_CALENDAR,
                               runner=context.runner, log=context.log)
    return windowed(store, account, start, end)


BACKEND = BackendSpec(
    key="apple",
    title="Apple Calendar (Calendar.app)",
    settings=(SettingSpec(CALENDAR_SETTING, f"calendar in Calendar.app to sync (default {DEFAULT_CALENDAR}; "
                                            "created when missing)", required=False, default=DEFAULT_CALENDAR),),
    build=build,
    notes="Drives Calendar.app through osascript; macOS asks for Automation permission on the first run. "
          "Busy status, privacy and categories are not scriptable: they stay at their defaults in Calendar.",
)

__all__ = ["AppleCalendarStore", "BACKEND", "build", "event_arguments", "event_record", "event_span",
           "reminder_from_trigger", "LIST_SCRIPT", "CREATE_SCRIPT", "UPDATE_SCRIPT", "DELETE_SCRIPT",
           "DEFAULT_CALENDAR", "CALENDAR_SETTING"]
