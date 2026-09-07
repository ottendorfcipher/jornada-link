"""An in-memory WebDAV / CalDAV / CardDAV server for the ``jornada.webapi.dav`` tests.

Runs ``http.server`` on 127.0.0.1 (ephemeral port) in a daemon thread.  It is
deliberately narrow: one principal, two calendars plus a plain collection, one
address book, ETag/If-Match handling, the four REPORTs the client sends and a
sync-collection that can be switched off (403) to exercise the fallback path.
The multistatus bodies use different namespace prefix spellings on purpose.
"""
from __future__ import annotations

import base64
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from jornada.webapi import ical

USERNAME = "alice"
PASSWORD = "s3cret"
SYNC_TOKEN = "http://fake.example/sync/42"
DELETED_HREF = "/cal/work/gone.ics"
NS_CALDAV = "urn:ietf:params:xml:ns:caldav"
NS_CARDDAV = "urn:ietf:params:xml:ns:carddav"

EVENT_A = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//fake//EN\r\nBEGIN:VEVENT\r\nUID:a\r\n"
    "DTSTAMP:20260301T000000Z\r\nDTSTART:20260310T090000Z\r\nDTEND:20260310T100000Z\r\n"
    "SUMMARY:March meeting\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)
EVENT_B = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//fake//EN\r\nBEGIN:VEVENT\r\nUID:b\r\n"
    "DTSTAMP:20260301T000000Z\r\nDTSTART:20260420T090000Z\r\nDTEND:20260420T100000Z\r\n"
    "SUMMARY:April meeting\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)
CARD_1 = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c1\r\nN:Doe;Jane;;;\r\nFN:Jane Doe\r\nEND:VCARD\r\n"


@dataclass(frozen=True)
class Item:
    etag: str
    data: str
    content_type: str


def _multistatus(inner: str, extra: str = "") -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<A:multistatus xmlns:A="DAV:" xmlns:B="{NS_CALDAV}" '
            f'xmlns:Z="{NS_CARDDAV}" xmlns:CS="http://calendarserver.org/ns/">{inner}{extra}</A:multistatus>').encode()


def _ok(href: str, props: str) -> str:
    return (f"<A:response><A:href>{escape(href)}</A:href><A:propstat><A:prop>{props}</A:prop>"
            f"<A:status>HTTP/1.1 200 OK</A:status></A:propstat></A:response>")


def _missing(href: str) -> str:
    return f"<A:response><A:href>{escape(href)}</A:href><A:status>HTTP/1.1 404 Not Found</A:status></A:response>"


class FakeDavServer(HTTPServer):
    """The server; ``items`` is the mutable store, ``requests`` records what arrived."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FakeDavHandler)
        self.items: Dict[str, Item] = {
            "/cal/work/a.ics": Item('"etag-a-1"', EVENT_A, "text/calendar; charset=utf-8"),
            "/cal/work/b.ics": Item('"etag-b-1"', EVENT_B, "text/calendar; charset=utf-8"),
            "/card/default/c1.vcf": Item('"etag-c1"', CARD_1, "text/vcard; charset=utf-8"),
        }
        self.sync_enabled = True
        self.principal_enabled = True
        self.requests: List[Tuple[str, str, Dict[str, str]]] = []
        self.etag_counter = 1
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"

    def start(self) -> "FakeDavServer":
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def next_etag(self) -> str:
        self.etag_counter += 1
        return f'"etag-{self.etag_counter}"'


class FakeDavHandler(BaseHTTPRequestHandler):
    server: FakeDavServer

    def log_message(self, *_args) -> None:  # keep pytest output clean
        pass

    # -- helpers --

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _begin(self) -> Optional[bytes]:
        """Record the request, read its body and enforce Basic auth; None means 'already answered'."""
        body = self._read_body()
        self.server.requests.append((self.command, self.path, {k: v for k, v in self.headers.items()}))
        expected = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self._reply(401, b"Unauthorized", (("WWW-Authenticate", 'Basic realm="fake"'),))
            return None
        return body

    def _reply(self, status: int, body: bytes = b"", headers: Tuple[Tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _xml(self, status: int, body: bytes) -> None:
        self._reply(status, body, (("Content-Type", 'application/xml; charset="utf-8"'),))

    def _members(self, prefix: str) -> List[Tuple[str, Item]]:
        return sorted((h, i) for h, i in self.server.items.items() if h.startswith(prefix))

    # -- verbs --

    def do_OPTIONS(self) -> None:
        if self._begin() is None:
            return
        self._reply(200, b"", (("DAV", "1, 3, extended-mkcol"), ("DAV", "calendar-access, addressbook"), ("Allow", "OPTIONS, PROPFIND, REPORT")))

    def do_PROPFIND(self) -> None:
        if self._begin() is None:
            return
        depth = self.headers.get("Depth", "0")
        if self.path == "/.well-known/caldav":
            self._reply(301, b"", (("Location", "/"),))
        elif self.path in ("/", "/.well-known/carddav") and self.server.principal_enabled:
            self._xml(207, _multistatus(_ok("/", "<A:current-user-principal><A:href>/principals/u/</A:href></A:current-user-principal>")))
        elif self.path == "/principals/u/" and self.server.principal_enabled:
            self._xml(207, self._principal_body())
        elif self.path == "/cal/" and depth == "1":
            self._xml(207, self._calendar_home_body())
        elif self.path == "/card/" and depth == "1":
            self._xml(207, _multistatus(_ok("/card/", "<A:resourcetype><A:collection/></A:resourcetype>") + self._addressbook_props()))
        elif self.path in ("/cal/work/", "/cal/tasks/", "/card/default/"):
            self._xml(207, self._collection_listing(self.path, depth))
        else:
            self._reply(404, b"Not Found")

    def _principal_body(self) -> bytes:
        return (b'<?xml version="1.0"?><multistatus xmlns="DAV:"><response><href>/principals/u/</href><propstat><prop>'
                b'<calendar-home-set xmlns="urn:ietf:params:xml:ns:caldav"><href xmlns="DAV:">/cal/</href></calendar-home-set>'
                b'<addressbook-home-set xmlns="urn:ietf:params:xml:ns:carddav"><href xmlns="DAV:">/card/</href></addressbook-home-set>'
                b'</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>')

    def _calendar_home_body(self) -> bytes:
        work = ("<A:displayname>Work</A:displayname><A:resourcetype><A:collection/><B:calendar/></A:resourcetype>"
                "<CS:getctag>ctag-work-7</CS:getctag><B:supported-calendar-component-set><B:comp name=\"VEVENT\"/>"
                "<B:comp name=\"VTODO\"/></B:supported-calendar-component-set>")
        tasks = ("<A:displayname>Tasks</A:displayname><A:resourcetype><A:collection/><B:calendar/></A:resourcetype>"
                 "<CS:getctag>ctag-tasks-2</CS:getctag><B:supported-calendar-component-set><B:comp name=\"VTODO\"/>"
                 "</B:supported-calendar-component-set>")
        notes = "<A:displayname>Notes</A:displayname><A:resourcetype><A:collection/></A:resourcetype>"
        unknown = ("<A:response><A:href>/cal/work/</A:href><A:propstat><A:prop><A:quota-used-bytes/></A:prop>"
                   "<A:status>HTTP/1.1 404 Not Found</A:status></A:propstat></A:response>")
        return _multistatus(_ok("/cal/", "<A:resourcetype><A:collection/></A:resourcetype>") + _ok("/cal/work/", work)
                            + _ok("/cal/tasks/", tasks) + _ok("/cal/notes/", notes) + unknown)

    def _addressbook_props(self) -> str:
        return _ok("/card/default/", "<A:displayname>Contacts</A:displayname><A:resourcetype><A:collection/><Z:addressbook/>"
                                     "</A:resourcetype><CS:getctag>ctag-card-3</CS:getctag>")

    def _collection_listing(self, path: str, depth: str) -> bytes:
        kind = "<Z:addressbook/>" if path.startswith("/card/") else "<B:calendar/>"
        own = _ok(path, f"<A:resourcetype><A:collection/>{kind}</A:resourcetype><A:displayname>Self</A:displayname>")
        if depth == "0":
            return _multistatus(own)
        members = "".join(
            _ok(href, f"<A:getetag>{escape(item.etag)}</A:getetag><A:getcontenttype>{escape(item.content_type)}</A:getcontenttype>"
                      "<A:resourcetype/>")
            for href, item in self._members(path)
        )
        sub = _ok(path + "sub/", "<A:resourcetype><A:collection/></A:resourcetype>")
        return _multistatus(own + members + sub)

    def do_GET(self) -> None:
        if self._begin() is None:
            return
        item = self.server.items.get(self.path)
        if item is None:
            self._reply(404, b"Not Found")
            return
        self._reply(200, item.data.encode(), (("ETag", item.etag), ("Content-Type", item.content_type)))

    def do_PUT(self) -> None:
        body = self._begin()
        if body is None:
            return
        existing = self.server.items.get(self.path)
        if_match = self.headers.get("If-Match")
        if if_match is not None and (existing is None or existing.etag != if_match):
            self._reply(412, b"Precondition Failed")
            return
        if self.headers.get("If-None-Match") == "*" and existing is not None:
            self._reply(412, b"Precondition Failed")
            return
        item = Item(self.server.next_etag(), body.decode("utf-8"), self.headers.get("Content-Type", "application/octet-stream"))
        self.server.items[self.path] = item
        self._reply(204 if existing else 201, b"", (("ETag", item.etag),))

    def do_DELETE(self) -> None:
        if self._begin() is None:
            return
        existing = self.server.items.get(self.path)
        if existing is None:
            self._reply(404, b"Not Found")
            return
        if_match = self.headers.get("If-Match")
        if if_match is not None and existing.etag != if_match:
            self._reply(412, b"Precondition Failed")
            return
        del self.server.items[self.path]
        self._reply(204)

    def do_REPORT(self) -> None:
        body = self._begin()
        if body is None:
            return
        root = ET.fromstring(body)
        local = root.tag.rsplit("}", 1)[-1]
        handlers = {
            "calendar-query": self._calendar_query, "calendar-multiget": self._multiget,
            "addressbook-query": self._addressbook_query, "addressbook-multiget": self._multiget,
            "sync-collection": self._sync_collection,
        }
        handler = handlers.get(local)
        if handler is None:
            self._reply(400, b"unsupported report")
            return
        handler(root)

    def _data_response(self, href: str, item: Item) -> str:
        tag = "Z:address-data" if href.startswith("/card/") else "B:calendar-data"
        return _ok(href, f"<A:getetag>{escape(item.etag)}</A:getetag><{tag}>{escape(item.data)}</{tag}>")

    def _calendar_query(self, root: ET.Element) -> None:
        time_range = root.find(f".//{{{NS_CALDAV}}}time-range")
        start = ical.parse_datetime_value(time_range.get("start")) if time_range is not None and time_range.get("start") else None
        end = ical.parse_datetime_value(time_range.get("end")) if time_range is not None and time_range.get("end") else None
        chosen = [(h, i) for h, i in self._members(self.path) if _in_range(i.data, start, end)]
        self._xml(207, _multistatus("".join(self._data_response(h, i) for h, i in chosen)))

    def _addressbook_query(self, _root: ET.Element) -> None:
        self._xml(207, _multistatus("".join(self._data_response(h, i) for h, i in self._members(self.path))))

    def _multiget(self, root: ET.Element) -> None:
        hrefs = [(el.text or "").strip() for el in root.findall("{DAV:}href")]
        parts = [self._data_response(h, self.server.items[h]) if h in self.server.items else _missing(h) for h in hrefs]
        self._xml(207, _multistatus("".join(parts)))

    def _sync_collection(self, _root: ET.Element) -> None:
        if not self.server.sync_enabled:
            self._xml(403, b'<?xml version="1.0"?><D:error xmlns:D="DAV:"><D:valid-sync-token/></D:error>')
            return
        changed = "".join(_ok(h, f"<A:getetag>{escape(i.etag)}</A:getetag>") for h, i in self._members(self.path))
        self._xml(207, _multistatus(changed + _missing(DELETED_HREF), f"<A:sync-token>{SYNC_TOKEN}</A:sync-token>"))


def _in_range(data: str, start, end) -> bool:
    event = ical.parse(data).find("VEVENT")
    if event is None:
        return False
    dtstart = ical.event_span(event)[0]
    return (start is None or dtstart >= start) and (end is None or dtstart < end)
