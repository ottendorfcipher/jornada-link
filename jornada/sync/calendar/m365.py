"""Backend "m365": the calendar of a Microsoft 365 / Exchange Online mailbox through Microsoft Graph.

Timed events are exchanged in UTC (``Prefer: outlook.timezone="UTC"`` on reads,
``timeZone: UTC`` on writes) and converted to the Mac's wall-clock here. All-day
events are written at midnight in the Mac's own zone when its IANA name is known,
which is how Outlook shows them on the right day. Bodies come back as HTML and
are flattened to text; the device's notes go up as text.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from ...pim.models import Appointment
from ...pim.timeconv import midnight, parse_iso
from ...webapi import ical
from ...webapi.http import HttpClient
from ...webapi.oauth import microsoft
from ...webapi.oauth_accounts import api_client, login_account, oauth_settings
from ..accounts import Account, AccountError
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import (ONE_DAY, Log, all_day_end, json_call, json_object, local_aware, local_wall_clock,
                     local_zone_name, nearest_midnight, send, string_field, text_of)
from .htmltext import html_to_text
from .window import resolve_window, windowed

SERVICE = "Microsoft 365"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ("Calendars.ReadWrite", "offline_access")
TENANT_SETTING = "tenant"
CALENDAR_SETTING = "calendar"
DEFAULT_TENANT = "common"
DEFAULT_EVENTS_PATH = "/me/calendar/events"
CALENDARS_PATH = "/me/calendars"
PAGE_SIZE = 100
MAX_PAGES = 400
SELECT_FIELDS = ("id,subject,body,start,end,isAllDay,location,showAs,sensitivity,isReminderOn,"
                 "reminderMinutesBeforeStart,categories,recurrence,type,changeKey,iCalUId")
PREFER_UTC = ("Prefer", 'outlook.timezone="UTC"')
GRAPH_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
SHOW_AS_TO_BUSY = {"free": "free", "tentative": "tentative", "busy": "busy", "oof": "out_of_office",
                   "workingElsewhere": "busy"}
BUSY_TO_SHOW_AS = {"free": "free", "tentative": "tentative", "busy": "busy", "out_of_office": "oof"}
RECURRING_TYPES = ("seriesMaster", "occurrence", "exception")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_FRACTION = re.compile(r"\.(\d+)(?=$|[Z+-])")
ZoneName = Callable[[], Optional[str]]


# -- mapping (pure) ---------------------------------------------------------------
def graph_moment(value: Any) -> datetime:
    """A Graph ``dateTimeTimeZone`` → aware datetime (floating when the zone name is unknown)."""
    if not isinstance(value, dict) or not isinstance(value.get("dateTime"), str):
        raise ValueError("event time is missing")
    text = _FRACTION.sub(lambda match: "." + match.group(1)[:6], value["dateTime"].strip())
    moment = parse_iso(text)
    if not isinstance(moment, datetime):
        moment = midnight(moment)
    if moment.tzinfo is not None:
        return moment
    zone = ical.resolve_zone(text_of(value.get("timeZone")) or "UTC")
    return moment if zone is None else moment.replace(tzinfo=zone)


def event_span(event: Dict[str, Any]) -> Tuple[datetime, datetime, bool]:
    """``(start, end, all_day)`` as naive local datetimes (ValueError when the times are unusable)."""
    all_day = bool(event.get("isAllDay"))
    first = local_wall_clock(graph_moment(event.get("start")))
    last = local_wall_clock(graph_moment(event.get("end"))) if event.get("end") else first
    if all_day:
        start, end = nearest_midnight(first), nearest_midnight(last)
        return start, (end if end > start else start + ONE_DAY), True
    return first, max(last, first), False


def body_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    content = text_of(body.get("content"))
    return html_to_text(content) if text_of(body.get("contentType")).lower() == "html" else content


def reminder_of(event: Dict[str, Any]) -> Optional[int]:
    if not event.get("isReminderOn"):
        return None
    try:
        return max(int(event.get("reminderMinutesBeforeStart", 0)), 0)
    except (TypeError, ValueError):
        return None


def event_to_appointment(event: Dict[str, Any]) -> Appointment:
    """A Graph event resource → Appointment (ValueError on a malformed one)."""
    start, end, all_day = event_span(event)
    location = event.get("location")
    return Appointment(
        summary=text_of(event.get("subject")),
        start=start,
        end=end,
        all_day=all_day,
        location=text_of(location.get("displayName")) if isinstance(location, dict) else "",
        notes=body_text(event.get("body")),
        categories=tuple(c for c in (event.get("categories") or ()) if isinstance(c, str) and c.strip()),
        busy_status=SHOW_AS_TO_BUSY.get(text_of(event.get("showAs")), "busy"),
        private=event.get("sensitivity") in ("private", "confidential"),
        reminder_minutes=reminder_of(event),
        recurring=event.get("recurrence") is not None or event.get("type") in RECURRING_TYPES,
        uid=text_of(event.get("iCalUId")) or text_of(event.get("id")),
    )


def utc_text(moment: datetime) -> str:
    """A naive local moment as Graph's UTC ``YYYY-MM-DDTHH:MM:SS`` text."""
    return local_aware(moment).astimezone(timezone.utc).strftime(GRAPH_TIME_FORMAT)


def appointment_to_event(record: Appointment, zone_name: Optional[str] = None) -> Dict[str, Any]:
    """Appointment → the JSON body of a Graph event (all-day events at midnight in ``zone_name``)."""
    appt = record.normalized()
    if appt.all_day:
        zone = zone_name or "UTC"
        start = {"dateTime": midnight(appt.start).strftime(GRAPH_TIME_FORMAT), "timeZone": zone}
        end = {"dateTime": all_day_end(appt.start, appt.end).strftime(GRAPH_TIME_FORMAT), "timeZone": zone}
    else:
        start = {"dateTime": utc_text(appt.start), "timeZone": "UTC"}
        end = {"dateTime": utc_text(max(appt.end, appt.start)), "timeZone": "UTC"}
    body = {
        "subject": appt.summary,
        "body": {"contentType": "text", "content": appt.notes},
        "start": start,
        "end": end,
        "isAllDay": appt.all_day,
        "location": {"displayName": appt.location},
        "showAs": BUSY_TO_SHOW_AS.get(appt.busy_status, "busy"),
        "sensitivity": "private" if appt.private else "normal",
        "isReminderOn": appt.reminder_minutes is not None,
        "categories": list(appt.categories),
    }
    if appt.reminder_minutes is None:
        return body
    return {**body, "reminderMinutesBeforeStart": max(int(appt.reminder_minutes), 0)}


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip():
        raise StoreError(f"{SERVICE} needs an event id to update or delete")


# -- the store ----------------------------------------------------------------------
class GraphCalendarStore:
    """Store protocol over the events of one Outlook calendar inside the window."""

    name = "m365-calendar"

    def __init__(self, http: HttpClient, start: datetime, end: datetime, calendar_name: str = "",
                 log: Log = lambda _line: None, zone_name: ZoneName = local_zone_name) -> None:
        self._http = http
        self._start = start
        self._end = end
        self._calendar_name = calendar_name.strip()
        self._log = log
        self._zone_name = zone_name
        self._events_path: Optional[str] = None
        self._recurring: Set[str] = set()

    def events_path(self) -> str:
        """``/me/calendar/events`` or the events of the calendar called ``calendar_name``."""
        if self._events_path is None:
            self._events_path = DEFAULT_EVENTS_PATH if not self._calendar_name else self._find_calendar()
        return self._events_path

    def _find_calendar(self) -> str:
        wanted = self._calendar_name.casefold()
        names: List[str] = []
        for entry in self._pages(CALENDARS_PATH, {"$select": "id,name", "$top": PAGE_SIZE}, "list the calendars"):
            name = text_of(entry.get("name"))
            names.append(name)
            if name.casefold() == wanted and text_of(entry.get("id")):
                return f"{CALENDARS_PATH}/{quote(entry['id'], safe='')}/events"
        raise StoreError(f"{SERVICE}: no calendar called {self._calendar_name!r} (found: {', '.join(names) or 'none'})")

    def _pages(self, path: str, params: Optional[Dict[str, Any]], what: str) -> List[Dict[str, Any]]:
        """Every ``value`` entry of a paged listing, following ``@odata.nextLink``."""
        entries: List[Dict[str, Any]] = []
        url: Optional[str] = path
        for _page in range(MAX_PAGES):
            if not url:
                return entries
            payload = json_object(json_call(self._http, SERVICE, "GET", url, what, params=params,
                                            headers=(PREFER_UTC,)), SERVICE, what)
            entries.extend(entry for entry in payload.get("value") or () if isinstance(entry, dict))
            url, params = text_of(payload.get("@odata.nextLink")) or None, None
        raise StoreError(f"{SERVICE}: the listing did not end after {MAX_PAGES} pages")

    # -- Store protocol ---------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        window = f"start/dateTime ge '{utc_text(self._start)}' and start/dateTime le '{utc_text(self._end)}'"
        params = {"$filter": window, "$top": PAGE_SIZE, "$select": SELECT_FIELDS}
        items = (self._item(entry) for entry in self._pages(self.events_path(), params, "list the events"))
        return tuple(item for item in items if item is not None)

    def _item(self, entry: Dict[str, Any]) -> Optional[Item]:
        item_id = text_of(entry.get("id"))
        if not item_id:
            return None
        version = text_of(entry.get("changeKey")) or None
        try:
            record = event_to_appointment(entry)
        except (ValueError, TypeError, OverflowError) as exc:
            self._log(f"{SERVICE} event {item_id} cannot be read ({exc}); it is left alone")
            return Item(id=item_id, record=None, version=version, problem=str(exc))
        self._remember(item_id, record.recurring)
        return Item(id=item_id, record=record, version=version, read_only=record.recurring)

    def _remember(self, item_id: str, recurring: bool) -> None:
        if recurring:
            self._recurring.add(item_id)
        else:
            self._recurring.discard(item_id)

    def create(self, record: Appointment) -> str:
        payload = json_call(self._http, SERVICE, "POST", self.events_path(), f"create the event {record.summary!r}",
                            json_body=appointment_to_event(record, self._zone_name()))
        return string_field(payload, SERVICE, "create")

    def update(self, item_id: str, record: Appointment) -> Optional[str]:
        _check_id(item_id)
        if item_id in self._recurring:
            raise StoreError(f"{record.summary!r} is a recurring series in {SERVICE}; it is left unchanged "
                             "(change it in Outlook)")
        payload = json_call(self._http, SERVICE, "PATCH", f"/me/events/{quote(item_id, safe='')}",
                            f"update the event {record.summary!r}", json_body=appointment_to_event(record, self._zone_name()))
        return text_of(json_object(payload, SERVICE, "update").get("changeKey")) or None

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        response = send(self._http, SERVICE, "DELETE", f"/me/events/{quote(item_id, safe='')}", "delete the event")
        if response.status == 404:
            self._log(f"{SERVICE} event {item_id} was already gone")
        elif not response.ok:
            raise StoreError(f"{SERVICE}: could not delete the event: HTTP {response.status}")
        self._recurring.discard(item_id)


# -- backend spec -------------------------------------------------------------------
def tenant_of(account: Account) -> str:
    tenant = (account.setting(TENANT_SETTING) or "").strip() or DEFAULT_TENANT
    if not _TENANT.match(tenant):
        raise AccountError(f"account {account.name!r}: {TENANT_SETTING} must be a tenant id or domain, not {tenant!r}")
    return tenant


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Any:
    start, end = resolve_window(account, context)
    http = api_client(GRAPH_BASE, microsoft(tenant_of(account)), SCOPES, account, secrets, context)
    store = GraphCalendarStore(http, start, end, calendar_name=account.setting(CALENDAR_SETTING) or "",
                               log=context.log)
    return windowed(store, account, start, end)


def login(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Dict[str, Any]:
    return login_account(microsoft(tenant_of(account)), SCOPES, account, secrets, context)


BACKEND = BackendSpec(
    key="m365",
    title="Microsoft 365 / Exchange Online calendar",
    settings=oauth_settings("Microsoft") + (
        SettingSpec(TENANT_SETTING, f"Azure AD tenant id or domain (default {DEFAULT_TENANT})", required=False),
        SettingSpec(CALENDAR_SETTING, "calendar display name (default: the mailbox's default calendar)", required=False),
    ),
    build=build,
    login=login,
    notes="Needs an Azure app registration (public client) with Calendars.ReadWrite. Event bodies are "
          "flattened to plain text on the way to the device.",
)

__all__ = ["GraphCalendarStore", "BACKEND", "build", "login", "tenant_of", "graph_moment", "event_span",
           "event_to_appointment", "appointment_to_event", "body_text", "reminder_of", "utc_text", "SERVICE",
           "GRAPH_BASE", "SCOPES", "SELECT_FIELDS", "PREFER_UTC", "DEFAULT_EVENTS_PATH", "CALENDARS_PATH"]
