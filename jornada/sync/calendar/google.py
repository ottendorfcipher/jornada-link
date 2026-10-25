"""Backend "google": one Google Calendar through the Calendar API v3.

Times travel as RFC 3339 with the Mac's UTC offset (no ``timeZone``), all-day
events as dates. Google has no categories and only knows free / opaque, so the
device's categories and its finer busy states ride along in the event's private
extended properties and come back intact; a "transparent" event is always free.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from ...pim.models import Appointment
from ...pim.timeconv import midnight, parse_iso
from ...webapi.http import HttpClient
from ...webapi.oauth import GOOGLE
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import (ONE_DAY, Log, all_day_end, iso_offset, json_call, json_object, local_wall_clock, send,
                     string_field, text_of)
from .window import resolve_window, windowed

SERVICE = "Google Calendar"
API_BASE = "https://www.googleapis.com/calendar/v3"
SCOPES = ("https://www.googleapis.com/auth/calendar.events",)
CALENDAR_SETTING = "calendar_id"
DEFAULT_CALENDAR = "primary"
PAGE_SIZE = 250
MAX_PAGES = 400
MAX_REMINDER_MINUTES = 40_320   # Google's limit: four weeks
CATEGORIES_KEY = "jornadaCategories"
BUSY_KEY = "jornadaBusy"
FINE_BUSY_STATES = ("tentative", "out_of_office")
GONE_STATUSES = (404, 410)


# -- mapping (pure) --------------------------------------------------------------
def event_to_appointment(event: Dict[str, Any]) -> Appointment:
    """A Calendar API event resource → Appointment (ValueError on a malformed one)."""
    start, end, all_day = event_span(event.get("start"), event.get("end"))
    private = private_properties(event)
    return Appointment(
        summary=text_of(event.get("summary")),
        start=start,
        end=end,
        all_day=all_day,
        location=text_of(event.get("location")),
        notes=text_of(event.get("description")),
        categories=tuple(part.strip() for part in text_of(private.get(CATEGORIES_KEY)).split(",") if part.strip()),
        busy_status=busy_status_of(event, private),
        private=event.get("visibility") in ("private", "confidential"),
        reminder_minutes=reminder_of(event.get("reminders")),
        recurring=bool(event.get("recurrence")) or bool(event.get("recurringEventId")),
        uid=text_of(event.get("iCalUID")) or text_of(event.get("id")),
    )


def event_span(start: Any, end: Any) -> Tuple[datetime, datetime, bool]:
    """``(start, end, all_day)`` as naive local datetimes from the event's start/end objects."""
    if not isinstance(start, dict):
        raise ValueError("event has no start")
    end = end if isinstance(end, dict) else {}
    if "date" in start:
        first = midnight(parse_iso(str(start["date"])))
        last = midnight(parse_iso(str(end["date"]))) if "date" in end else first + ONE_DAY
        return first, (last if last > first else first + ONE_DAY), True
    first = _moment(start.get("dateTime"))
    last = _moment(end.get("dateTime")) if end.get("dateTime") else first
    return first, max(last, first), False


def _moment(text: Any) -> datetime:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("event time is missing")
    moment = parse_iso(text)
    return local_wall_clock(moment if isinstance(moment, datetime) else midnight(moment))


def reminder_of(reminders: Any) -> Optional[int]:
    """Minutes of the first popup override; None for the calendar's defaults or no reminder."""
    if not isinstance(reminders, dict) or reminders.get("useDefault"):
        return None
    for override in reminders.get("overrides") or ():
        if isinstance(override, dict) and override.get("method") == "popup":
            try:
                return max(int(override.get("minutes", 0)), 0)
            except (TypeError, ValueError):
                continue
    return None


def private_properties(event: Dict[str, Any]) -> Dict[str, Any]:
    extended = event.get("extendedProperties")
    private = extended.get("private") if isinstance(extended, dict) else None
    return private if isinstance(private, dict) else {}


def busy_status_of(event: Dict[str, Any], private: Dict[str, Any]) -> str:
    if event.get("transparency") == "transparent":
        return "free"
    remembered = text_of(private.get(BUSY_KEY))
    return remembered if remembered in FINE_BUSY_STATES else "busy"


def appointment_to_event(record: Appointment) -> Dict[str, Any]:
    """Appointment → the JSON body of a Calendar API event."""
    appt = record.normalized()
    if appt.all_day:
        start = {"date": appt.start.date().isoformat()}
        end = {"date": all_day_end(appt.start, appt.end).date().isoformat()}
    else:
        start, end = {"dateTime": iso_offset(appt.start)}, {"dateTime": iso_offset(max(appt.end, appt.start))}
    return {
        "summary": appt.summary,
        "location": appt.location,
        "description": appt.notes,
        "start": start,
        "end": end,
        "transparency": "transparent" if appt.busy_status == "free" else "opaque",
        "visibility": "private" if appt.private else "default",
        "reminders": _reminders(appt.reminder_minutes),
        "extendedProperties": {"private": {CATEGORIES_KEY: ",".join(appt.categories), BUSY_KEY: appt.busy_status}},
    }


def _reminders(minutes: Optional[int]) -> Dict[str, Any]:
    if minutes is None:
        return {"useDefault": True}
    bounded = min(max(int(minutes), 0), MAX_REMINDER_MINUTES)
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": bounded}]}


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip():
        raise StoreError(f"{SERVICE} needs an event id to update or delete")


# -- the store ---------------------------------------------------------------------
class GoogleCalendarStore:
    """Store protocol over the events of one Google calendar inside the window."""

    name = "google-calendar"

    def __init__(self, http: HttpClient, start: datetime, end: datetime, calendar_id: str = DEFAULT_CALENDAR,
                 log: Log = lambda _line: None) -> None:
        self._http = http
        self._start = start
        self._end = end
        self._calendar = calendar_id.strip() or DEFAULT_CALENDAR
        self._log = log
        self._recurring: Set[str] = set()

    @property
    def calendar_id(self) -> str:
        return self._calendar

    def _events_path(self) -> str:
        return f"/calendars/{quote(self._calendar, safe='')}/events"

    def _event_path(self, item_id: str) -> str:
        return f"{self._events_path()}/{quote(item_id, safe='')}"

    # -- Store protocol ---------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        items: List[Item] = []
        token: Optional[str] = None
        for _page in range(MAX_PAGES):
            payload = self._page(token)
            items.extend(item for item in map(self._item, payload.get("items") or ()) if item is not None)
            token = text_of(payload.get("nextPageToken")) or None
            if not token:
                return tuple(items)
        raise StoreError(f"{SERVICE}: the event listing did not end after {MAX_PAGES} pages")

    def _page(self, token: Optional[str]) -> Dict[str, Any]:
        params = {"timeMin": iso_offset(self._start), "timeMax": iso_offset(self._end), "singleEvents": "false",
                  "showDeleted": "false", "maxResults": PAGE_SIZE, "pageToken": token}
        payload = json_call(self._http, SERVICE, "GET", self._events_path(), "list the events", params=params)
        return json_object(payload, SERVICE, "list the events")

    def _item(self, entry: Any) -> Optional[Item]:
        if not isinstance(entry, dict) or not text_of(entry.get("id")):
            return None
        if entry.get("status") == "cancelled":
            return None
        version = text_of(entry.get("etag")) or None
        try:
            record = event_to_appointment(entry)
        except (ValueError, TypeError, OverflowError) as exc:
            self._log(f"{SERVICE} event {entry['id']} cannot be read ({exc}); it is left alone")
            return Item(id=entry["id"], record=None, version=version, problem=str(exc))
        self._remember(entry["id"], record.recurring)
        return Item(id=entry["id"], record=record, version=version, read_only=record.recurring)

    def _remember(self, item_id: str, recurring: bool) -> None:
        if recurring:
            self._recurring.add(item_id)
        else:
            self._recurring.discard(item_id)

    def create(self, record: Appointment) -> str:
        payload = json_call(self._http, SERVICE, "POST", self._events_path(), f"create the event {record.summary!r}",
                            json_body=appointment_to_event(record))
        return string_field(payload, SERVICE, "create")

    def update(self, item_id: str, record: Appointment) -> Optional[str]:
        _check_id(item_id)
        if item_id in self._recurring:
            raise StoreError(f"{record.summary!r} is a recurring series in {SERVICE}; it is left unchanged "
                             "(change it in Google Calendar)")
        payload = json_call(self._http, SERVICE, "PUT", self._event_path(item_id),
                            f"update the event {record.summary!r}", json_body=appointment_to_event(record))
        return text_of(json_object(payload, SERVICE, "update").get("etag")) or None

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        response = send(self._http, SERVICE, "DELETE", self._event_path(item_id), "delete the event")
        if response.status in GONE_STATUSES:
            self._log(f"{SERVICE} event {item_id} was already gone")
        elif not response.ok:
            raise StoreError(f"{SERVICE}: could not delete the event: HTTP {response.status}")
        self._recurring.discard(item_id)


# -- backend spec ------------------------------------------------------------------
def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Any:
    start, end = resolve_window(account, context)
    http = api_client(API_BASE, GOOGLE, SCOPES, account, secrets, context)
    store = GoogleCalendarStore(http, start, end, calendar_id=account.setting(CALENDAR_SETTING) or DEFAULT_CALENDAR,
                                log=context.log)
    return windowed(store, account, start, end)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(GOOGLE, SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key="google",
    title="Google Calendar",
    settings=oauth_settings("Google") + (
        SettingSpec(CALENDAR_SETTING, f"calendar id to sync (default {DEFAULT_CALENDAR}; other calendars have "
                                      "ids like abc@group.calendar.google.com)", required=False, default=DEFAULT_CALENDAR),
    ),
    build=build,
    login=login,
    notes="Needs your own Google Cloud OAuth desktop client (calendar.events scope). Categories and the "
          "tentative / out-of-office states are kept in the event's private extended properties.",
)

__all__ = ["GoogleCalendarStore", "BACKEND", "build", "login", "event_to_appointment", "appointment_to_event",
           "event_span", "reminder_of", "busy_status_of", "SERVICE", "API_BASE", "SCOPES", "CATEGORIES_KEY",
           "BUSY_KEY"]
