"""WebDAV / CalDAV / CardDAV client built on :mod:`urllib.request`.

The HTTP transport is injectable (any callable ``Request -> Response``), so
the client can be exercised without a network.  The default transport is a
minimal urllib opener that speaks any method (PROPFIND, REPORT, ...), never
raises on HTTP status codes, only handles http(s) URLs and does not follow
redirects itself: :meth:`DavClient.request` follows them, refusing an
https → http downgrade.  Credentials travel only in the Authorization header
and never appear in error messages or reprs.

Property names are Clark notation (``{DAV:}getetag``); :data:`PROP_ETAG` and
friends name the ones this module uses.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import http.client
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

NS_DAV = "DAV:"
NS_CALDAV = "urn:ietf:params:xml:ns:caldav"
NS_CARDDAV = "urn:ietf:params:xml:ns:carddav"
NS_CALENDARSERVER = "http://calendarserver.org/ns/"

for _prefix, _uri in (("D", NS_DAV), ("C", NS_CALDAV), ("CR", NS_CARDDAV), ("CS", NS_CALENDARSERVER)):
    ET.register_namespace(_prefix, _uri)


def clark(namespace: str, name: str) -> str:
    """``{namespace}name``."""
    return "{%s}%s" % (namespace, name)


PROP_ETAG = clark(NS_DAV, "getetag")
PROP_CONTENT_TYPE = clark(NS_DAV, "getcontenttype")
PROP_RESOURCETYPE = clark(NS_DAV, "resourcetype")
PROP_DISPLAYNAME = clark(NS_DAV, "displayname")
PROP_CURRENT_USER_PRINCIPAL = clark(NS_DAV, "current-user-principal")
PROP_SYNC_TOKEN = clark(NS_DAV, "sync-token")
PROP_CTAG = clark(NS_CALENDARSERVER, "getctag")
PROP_CALENDAR_HOME_SET = clark(NS_CALDAV, "calendar-home-set")
PROP_CALENDAR_DATA = clark(NS_CALDAV, "calendar-data")
PROP_SUPPORTED_COMPONENTS = clark(NS_CALDAV, "supported-calendar-component-set")
PROP_ADDRESSBOOK_HOME_SET = clark(NS_CARDDAV, "addressbook-home-set")
PROP_ADDRESS_DATA = clark(NS_CARDDAV, "address-data")
TYPE_COLLECTION = clark(NS_DAV, "collection")
TYPE_CALENDAR = clark(NS_CALDAV, "calendar")
TYPE_ADDRESSBOOK = clark(NS_CARDDAV, "addressbook")

USER_AGENT = "jornada-link"
CONTENT_TYPE_XML = "application/xml; charset=utf-8"
XML_HEADERS = (("Content-Type", CONTENT_TYPE_XML),)
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
WELL_KNOWN_PATHS = ("/.well-known/caldav", "/.well-known/carddav")
COLLECTION_PROPS = (PROP_DISPLAYNAME, PROP_RESOURCETYPE, PROP_CTAG, PROP_SUPPORTED_COMPONENTS)
ITEM_PROPS = (PROP_ETAG, PROP_CONTENT_TYPE, PROP_RESOURCETYPE)

_REDIRECT_CODES = (301, 302, 303, 307, 308)
_UNSAFE_METHODS = ("PUT", "DELETE", "POST")
_STATUS_RE = re.compile(r"\b(\d{3})\b")
_COMPONENT_RE = re.compile(r"^[A-Z][A-Z0-9-]*$")
_TAG_RE = re.compile(r"<[^>]*>")
_OPEN_TAG_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)")

Headers = Tuple[Tuple[str, str], ...]


# --- errors and messages ----------------------------------------------------------

class DavError(Exception):
    """A request failed; ``status`` is the HTTP status (None for transport failures)."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class DavNotFound(DavError):
    """HTTP 404."""


class DavConflict(DavError):
    """HTTP 412: an If-Match / If-None-Match precondition failed."""


@dataclass(frozen=True)
class Request:
    """One outbound HTTP request as handed to the transport."""

    method: str
    url: str
    headers: Headers = ()
    body: Optional[bytes] = None

    def header(self, name: str) -> Optional[str]:
        return _first_header(self.headers, name)


@dataclass(frozen=True)
class Response:
    """One HTTP response; ``url`` is where the request ended after redirects."""

    status: int
    headers: Headers = ()
    body: bytes = b""
    url: Optional[str] = None

    def header(self, name: str) -> Optional[str]:
        return _first_header(self.headers, name)

    def header_values(self, name: str) -> Tuple[str, ...]:
        wanted = name.lower()
        return tuple(value for key, value in self.headers if key.lower() == wanted)


Transport = Callable[[Request], Response]


def _first_header(headers: Headers, name: str) -> Optional[str]:
    wanted = name.lower()
    return next((value for key, value in headers if key.lower() == wanted), None)


def safe_url(url: str) -> str:
    """``url`` with any ``user:password@`` removed, for messages and logs."""
    parts = urllib.parse.urlsplit(url)
    if not parts.username and not parts.password:
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc=_host_netloc(parts)))


def _host_netloc(parts: urllib.parse.SplitResult) -> str:
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parts.port}" if parts.port else host


# --- default transport --------------------------------------------------------------

def urllib_transport(timeout: float) -> Transport:
    """A transport over urllib: any method, no automatic redirects, http(s) only."""
    opener = _build_opener()

    def send(request: Request) -> Response:
        prepared = urllib.request.Request(
            request.url, data=request.body, method=request.method.upper(), headers=dict(request.headers)
        )
        try:
            with opener.open(prepared, timeout=timeout) as raw:
                return Response(int(raw.status), tuple(raw.headers.items()), _read_limited(raw))
        except urllib.error.HTTPError as exc:
            return Response(int(exc.code), tuple(exc.headers.items()), _read_error_body(exc))
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise DavError(f"{request.method} {safe_url(request.url)}: {_describe(exc)}") from exc

    return send


def _build_opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    ):
        opener.add_handler(handler)
    return opener


def _read_limited(raw: http.client.HTTPResponse) -> bytes:
    data = raw.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise DavError(f"response larger than {MAX_RESPONSE_BYTES} bytes")
    return data


def _read_error_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read() or b""
    except (OSError, ValueError, http.client.HTTPException):
        return b""


def _describe(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    return str(reason if reason is not None else exc) or type(exc).__name__


# --- the client -----------------------------------------------------------------------

class DavClient:
    """A WebDAV client bound to ``base_url`` with optional Basic or Bearer auth.

    ``https://user:pass@host/`` style credentials in ``base_url`` are honoured
    (and stripped from the stored URL) when no ``username`` is given.
    """

    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        bearer_token: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[Transport] = None,
    ) -> None:
        parts = urllib.parse.urlsplit(base_url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DavError(f"base URL must be http(s)://host/...: {safe_url(base_url)!r}")
        if username is None and parts.username:
            username = urllib.parse.unquote(parts.username)
            password = urllib.parse.unquote(parts.password or "")
        path = parts.path if parts.path.endswith("/") else parts.path + "/"
        self.base_url = urllib.parse.urlunsplit((parts.scheme, _host_netloc(parts), path, "", ""))
        self.timeout = timeout
        self._authorization = _authorization(username, password, bearer_token)
        self._send: Transport = transport if transport is not None else urllib_transport(timeout)

    def __repr__(self) -> str:
        return f"DavClient({self.base_url!r}, auth={'yes' if self._authorization else 'no'})"

    # -- plumbing --

    def resolve(self, path: str) -> str:
        """Absolute URL for an absolute URL, an absolute path or a path relative to the base."""
        target = urllib.parse.urljoin(self.base_url, path.strip())
        if urllib.parse.urlsplit(target).scheme not in ("http", "https"):
            raise DavError(f"refusing non-http(s) URL {safe_url(target)!r}")
        return target

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Iterable[Tuple[str, str]] = (),
        depth: Union[int, str, None] = None,
    ) -> Response:
        """Send one request, following same-scheme redirects; returns any status."""
        url = self.resolve(path)
        sent = self._headers(headers, depth)
        response = Response(0)
        for _ in range(MAX_REDIRECTS + 1):
            response = self._send(Request(method.upper(), url, sent, body))
            target = _redirect_target(method.upper(), response, url)
            if target is None:
                return dataclasses.replace(response, url=url)
            url = target
        raise DavError(f"{method} {safe_url(url)}: too many redirects", response.status)

    def _headers(self, extra: Iterable[Tuple[str, str]], depth: Union[int, str, None]) -> Headers:
        headers: List[Tuple[str, str]] = [("User-Agent", USER_AGENT), ("Accept", "*/*")]
        if self._authorization:
            headers.append(("Authorization", self._authorization))
        if depth is not None:
            headers.append(("Depth", str(depth)))
        return tuple(headers) + tuple(extra)

    # -- WebDAV verbs --

    def options(self, path: str = "") -> Tuple[str, ...]:
        """The server's ``DAV:`` compliance classes (``1``, ``3``, ``calendar-access``, ...)."""
        response = self.request("OPTIONS", path)
        _check(response, f"OPTIONS {safe_url(self.resolve(path))}", expected=(200, 204))
        return tuple(
            token.strip() for value in response.header_values("DAV") for token in value.split(",") if token.strip()
        )

    def propfind(self, path: str, props: Sequence[str], depth: Union[int, str] = 0) -> Tuple[DavResource, ...]:
        """PROPFIND for the Clark-named ``props``; returns one resource per response."""
        return self.propfind_at(path, props, depth)[1]

    def propfind_at(self, path: str, props: Sequence[str], depth: Union[int, str] = 0) -> Tuple[str, Tuple[DavResource, ...]]:
        """Like :meth:`propfind` but also returns the URL the request ended at (after redirects)."""
        response = self.request("PROPFIND", path, _propfind_body(props), XML_HEADERS, depth=depth)
        _check(response, f"PROPFIND {safe_url(self.resolve(path))}", expected=(207,))
        return response.url or self.resolve(path), parse_multistatus(response.body).responses

    def report(self, path: str, body: bytes, depth: Union[int, str, None] = 1) -> Tuple[DavResource, ...]:
        """REPORT with a prebuilt XML ``body``; ``depth=None`` omits the Depth header."""
        response = self.request("REPORT", path, body, XML_HEADERS, depth=depth)
        _check(response, f"REPORT {safe_url(self.resolve(path))}", expected=(207,))
        return parse_multistatus(response.body).responses

    def get(self, path: str) -> Tuple[Optional[str], bytes]:
        """``(etag, body)``; 404 raises :class:`DavNotFound`."""
        response = self.request("GET", path)
        _check(response, f"GET {safe_url(self.resolve(path))}", expected=(200,))
        return response.header("ETag"), response.body

    def put(
        self,
        path: str,
        body: bytes,
        content_type: str,
        if_match: Optional[str] = None,
        if_none_match: Optional[str] = None,
    ) -> Optional[str]:
        """Store ``body``; returns the new ETag if the server sent one; 412 raises :class:`DavConflict`."""
        headers = [("Content-Type", content_type)]
        if if_match is not None:
            headers.append(("If-Match", _etag_header(if_match)))
        if if_none_match is not None:
            headers.append(("If-None-Match", _etag_header(if_none_match)))
        response = self.request("PUT", path, bytes(body), headers)
        _check(response, f"PUT {safe_url(self.resolve(path))}", expected=(200, 201, 204))
        return response.header("ETag")

    def delete(self, path: str, if_match: Optional[str] = None) -> None:
        """Delete a resource; 404 raises :class:`DavNotFound`, 412 :class:`DavConflict`."""
        headers = [("If-Match", _etag_header(if_match))] if if_match is not None else []
        response = self.request("DELETE", path, None, headers)
        _check(response, f"DELETE {safe_url(self.resolve(path))}", expected=(200, 202, 204))


def _authorization(username: Optional[str], password: Optional[str], token: Optional[str]) -> Optional[str]:
    if token:
        return f"Bearer {token}"
    if username is not None:
        raw = f"{username}:{password or ''}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")
    return None


def _redirect_target(method: str, response: Response, url: str) -> Optional[str]:
    location = response.header("Location")
    if response.status not in _REDIRECT_CODES or not location:
        return None
    if method in _UNSAFE_METHODS and response.status in (301, 302, 303):
        return None
    target = urllib.parse.urljoin(url, location)
    scheme = urllib.parse.urlsplit(target).scheme
    if scheme not in ("http", "https"):
        raise DavError(f"{method} {safe_url(url)}: redirect to unsupported URL", response.status)
    if scheme == "http" and urllib.parse.urlsplit(url).scheme == "https":
        raise DavError(f"{method} {safe_url(url)}: refusing redirect from https to http", response.status)
    return target


def _etag_header(value: str) -> str:
    text = value.strip()
    if text == "*" or text.startswith('"') or text.startswith("W/"):
        return text
    return f'"{text}"'


def _check(response: Response, context: str, expected: Tuple[int, ...] = ()) -> None:
    status = response.status
    if status == 404:
        raise DavNotFound(f"{context}: not found (HTTP 404)", 404)
    if status == 412:
        raise DavConflict(f"{context}: precondition failed (HTTP 412)", 412)
    if status >= 400 or (expected and status not in expected):
        raise DavError(f"{context}: unexpected HTTP {status}{_excerpt(response.body)}", status)


def _excerpt(body: bytes, limit: int = 160) -> str:
    """A short, tag-free summary of an error body for messages (never the request)."""
    text = body[:4096].decode("utf-8", "replace")
    words = _TAG_RE.sub(" ", text).split()
    if not words:  # a WebDAV XML error carries its meaning in the element names
        words = [name for name in _OPEN_TAG_RE.findall(text) if name.lower() != "xml"]
    joined = " ".join(words)
    return f" ({joined[:limit]})" if joined else ""


# --- XML: building requests and reading multistatus -----------------------------------------

def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _propfind_body(props: Sequence[str]) -> bytes:
    root = ET.Element(clark(NS_DAV, "propfind"))
    prop = ET.SubElement(root, clark(NS_DAV, "prop"))
    for name in props:
        if not name.startswith("{"):
            raise DavError(f"property names must be Clark notation, got {name!r}")
        ET.SubElement(prop, name)
    return _xml_bytes(root)


@dataclass(frozen=True)
class DavResource:
    """One ``<D:response>``: its href, status and the successfully returned props.

    ``props`` maps Clark names to text.  ``resourcetype`` is the comma-joined
    list of its child element names; href-valued properties hold their first
    href; ``supported-calendar-component-set`` the comma-joined ``name``
    attributes.
    """

    href: str
    status: Optional[int]
    props: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "props", MappingProxyType(dict(self.props)))

    def prop(self, name: str) -> Optional[str]:
        return self.props.get(name)

    @property
    def resource_types(self) -> Tuple[str, ...]:
        return tuple(t for t in (self.props.get(PROP_RESOURCETYPE) or "").split(",") if t)

    def has_type(self, clark_name: str) -> bool:
        return clark_name in self.resource_types

    @property
    def is_collection(self) -> bool:
        return self.has_type(TYPE_COLLECTION)


@dataclass(frozen=True)
class Multistatus:
    """A parsed 207 body: the responses and, for sync-collection, the new token."""

    responses: Tuple[DavResource, ...]
    sync_token: Optional[str] = None


def parse_multistatus(body: bytes) -> Multistatus:
    """Parse a multistatus body, keeping only props from 2xx propstat blocks.

    Namespace prefixes may be spelled any way; a missing status counts as 200.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise DavError(f"malformed multistatus XML: {exc}") from exc
    responses = tuple(_parse_response(el) for el in root.iter(clark(NS_DAV, "response")))
    token_el = root.find(clark(NS_DAV, "sync-token"))
    token = (token_el.text or "").strip() if token_el is not None else None
    return Multistatus(responses, token or None)


def _parse_response(element: ET.Element) -> DavResource:
    href_el = element.find(clark(NS_DAV, "href"))
    href = (href_el.text or "").strip() if href_el is not None else ""
    status = _status_code(element.find(clark(NS_DAV, "status")))
    props: Dict[str, str] = {}
    fallback: Optional[int] = None
    for propstat in element.findall(clark(NS_DAV, "propstat")):
        code = _status_code(propstat.find(clark(NS_DAV, "status")))
        fallback = code if fallback is None else fallback
        if code is not None and not 200 <= code < 300:
            continue
        prop_el = propstat.find(clark(NS_DAV, "prop"))
        props.update({child.tag: _prop_value(child) for child in (prop_el if prop_el is not None else ())})
        fallback = code if code is not None else 200
    return DavResource(href, status if status is not None else fallback, props)


def _status_code(element: Optional[ET.Element]) -> Optional[int]:
    match = _STATUS_RE.search(element.text or "") if element is not None else None
    return int(match.group(1)) if match else None


def _prop_value(element: ET.Element) -> str:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    hrefs = [child for child in children if child.tag == clark(NS_DAV, "href")]
    if hrefs:
        return (hrefs[0].text or "").strip()
    names = [child.get("name") for child in children if child.get("name")]
    if names:
        return ",".join(names)
    return ",".join(child.tag for child in children)


# --- discovery ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Collection:
    """A calendar or address book collection."""

    href: str
    display_name: str
    ctag: Optional[str]
    components: Tuple[str, ...]
    kind: str


def discover_principal(client: DavClient) -> str:
    """Absolute URL of the current user's principal (base, well-known paths, then ``/``)."""
    failures: List[str] = []
    for path in ("", *WELL_KNOWN_PATHS, "/"):
        try:
            final_url, resources = client.propfind_at(path, (PROP_CURRENT_USER_PRINCIPAL,), depth=0)
        except DavError as exc:
            failures.append(f"{path or 'base'}: {exc.message}")
            continue
        href = next((r.prop(PROP_CURRENT_USER_PRINCIPAL) for r in resources if r.prop(PROP_CURRENT_USER_PRINCIPAL)), None)
        if href:
            return urllib.parse.urljoin(final_url, href)
        failures.append(f"{path or 'base'}: no current-user-principal")
    raise DavError("could not discover the principal: " + "; ".join(failures))


def discover_calendars(client: DavClient) -> Tuple[Collection, ...]:
    """Every calendar under the calendar home set (or the base URL when it is itself a calendar)."""
    return _discover_collections(client, PROP_CALENDAR_HOME_SET, TYPE_CALENDAR, "calendar")


def discover_addressbooks(client: DavClient) -> Tuple[Collection, ...]:
    """Every address book under the addressbook home set (or the base URL when it is one)."""
    return _discover_collections(client, PROP_ADDRESSBOOK_HOME_SET, TYPE_ADDRESSBOOK, "addressbook")


def _discover_collections(client: DavClient, home_prop: str, type_name: str, kind: str) -> Tuple[Collection, ...]:
    home = _find_home(client, home_prop)
    found = _collections_in(client, home, type_name, kind) if home else ()
    if found:
        return found
    base = _base_as_collection(client, type_name, kind)
    if base is not None:
        return (base,)
    if home is None:
        raise DavError(f"could not discover a {kind} home set and the base URL is not a {kind}")
    return ()


def _find_home(client: DavClient, home_prop: str) -> Optional[str]:
    try:
        principal = discover_principal(client)
        final_url, resources = client.propfind_at(principal, (home_prop,), depth=0)
    except DavError:
        return None
    href = next((r.prop(home_prop) for r in resources if r.prop(home_prop)), None)
    return urllib.parse.urljoin(final_url, href) if href else None


def _collections_in(client: DavClient, home: str, type_name: str, kind: str) -> Tuple[Collection, ...]:
    final_url, resources = client.propfind_at(home, COLLECTION_PROPS, depth=1)
    return tuple(_collection(r, kind, final_url) for r in resources if r.has_type(type_name))


def _base_as_collection(client: DavClient, type_name: str, kind: str) -> Optional[Collection]:
    try:
        final_url, resources = client.propfind_at("", COLLECTION_PROPS, depth=0)
    except DavError:
        return None
    match = next((r for r in resources if r.has_type(type_name)), None)
    return _collection(match, kind, final_url) if match is not None else None


def _collection(resource: DavResource, kind: str, relative_to: str) -> Collection:
    href = urllib.parse.urljoin(relative_to, resource.href)
    components = resource.prop(PROP_SUPPORTED_COMPONENTS) or ""
    return Collection(
        href=href,
        display_name=resource.prop(PROP_DISPLAYNAME) or _last_segment(href),
        ctag=resource.prop(PROP_CTAG),
        components=tuple(c.strip().upper() for c in components.split(",") if c.strip()),
        kind=kind,
    )


def _last_segment(href: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(href).path).rstrip("/")
    return path.rsplit("/", 1)[-1]


# --- items ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DavItem:
    """One calendar object or vCard resource; ``data`` is filled by the REPORTs only."""

    href: str
    etag: Optional[str]
    data: Optional[str]


def list_items(client: DavClient, collection_href: str) -> Tuple[DavItem, ...]:
    """Hrefs and ETags of the members of a collection (no data, no sub-collections)."""
    resources = client.propfind(collection_href, ITEM_PROPS, depth=1)
    own = _path_key(collection_href)
    return tuple(
        DavItem(r.href, r.prop(PROP_ETAG), None)
        for r in resources if _path_key(r.href) != own and not r.is_collection
    )


def calendar_query(
    client: DavClient,
    collection_href: str,
    component: str = "VEVENT",
    start: Optional[_dt.datetime] = None,
    end: Optional[_dt.datetime] = None,
) -> Tuple[DavItem, ...]:
    """CalDAV calendar-query for ``component`` objects, optionally within ``[start, end)``."""
    body = _calendar_query_body(component, start, end)
    return _items_with_data(client.report(collection_href, body, depth=1), PROP_CALENDAR_DATA, collection_href)


def calendar_multiget(client: DavClient, collection_href: str, hrefs: Sequence[str]) -> Tuple[DavItem, ...]:
    """Fetch the named calendar objects (with data) in one REPORT."""
    if not hrefs:
        return ()
    body = _multiget_body(clark(NS_CALDAV, "calendar-multiget"), PROP_CALENDAR_DATA, hrefs)
    return _items_with_data(client.report(collection_href, body, depth=1), PROP_CALENDAR_DATA, collection_href)


def addressbook_query(client: DavClient, collection_href: str) -> Tuple[DavItem, ...]:
    """CardDAV addressbook-query returning every vCard with its data."""
    root = ET.Element(clark(NS_CARDDAV, "addressbook-query"))
    _prop_request(root, PROP_ADDRESS_DATA)
    ET.SubElement(root, clark(NS_CARDDAV, "filter"))
    return _items_with_data(client.report(collection_href, _xml_bytes(root), depth=1), PROP_ADDRESS_DATA, collection_href)


def addressbook_multiget(client: DavClient, collection_href: str, hrefs: Sequence[str]) -> Tuple[DavItem, ...]:
    """Fetch the named vCards (with data) in one REPORT."""
    if not hrefs:
        return ()
    body = _multiget_body(clark(NS_CARDDAV, "addressbook-multiget"), PROP_ADDRESS_DATA, hrefs)
    return _items_with_data(client.report(collection_href, body, depth=1), PROP_ADDRESS_DATA, collection_href)


def sync_collection(
    client: DavClient,
    collection_href: str,
    sync_token: Optional[str],
) -> Tuple[Tuple[DavItem, ...], Tuple[str, ...], Optional[str]]:
    """RFC 6578 sync-collection: ``(changed items without data, deleted hrefs, new token)``.

    A server that rejects the token or lacks the report answers 403/507 (or
    omits the token), which raises :class:`DavError` so the caller can fall
    back to :func:`list_items`.
    """
    root = ET.Element(clark(NS_DAV, "sync-collection"))
    ET.SubElement(root, clark(NS_DAV, "sync-token")).text = sync_token or ""
    ET.SubElement(root, clark(NS_DAV, "sync-level")).text = "1"
    ET.SubElement(ET.SubElement(root, clark(NS_DAV, "prop")), PROP_ETAG)
    response = client.request("REPORT", collection_href, _xml_bytes(root), XML_HEADERS)
    context = f"REPORT sync-collection {safe_url(client.resolve(collection_href))}"
    _check(response, context, expected=(207,))
    result = parse_multistatus(response.body)
    if not result.sync_token:
        raise DavError(f"{context}: the server returned no sync token", response.status)
    own = _path_key(collection_href)
    members = [r for r in result.responses if _path_key(r.href) != own]
    deleted = tuple(r.href for r in members if r.status == 404 and not r.props)
    changed = tuple(DavItem(r.href, r.prop(PROP_ETAG), None) for r in members if not (r.status == 404 and not r.props) and not r.is_collection)
    return changed, deleted, result.sync_token


def _calendar_query_body(component: str, start: Optional[_dt.datetime], end: Optional[_dt.datetime]) -> bytes:
    name = component.strip().upper()
    if not _COMPONENT_RE.match(name):
        raise DavError(f"invalid component name {component!r}")
    root = ET.Element(clark(NS_CALDAV, "calendar-query"))
    _prop_request(root, PROP_CALENDAR_DATA)
    outer = ET.SubElement(ET.SubElement(root, clark(NS_CALDAV, "filter")), clark(NS_CALDAV, "comp-filter"), name="VCALENDAR")
    inner = ET.SubElement(outer, clark(NS_CALDAV, "comp-filter"), name=name)
    if start is not None or end is not None:
        attrs = {key: _utc_text(value) for key, value in (("start", start), ("end", end)) if value is not None}
        ET.SubElement(inner, clark(NS_CALDAV, "time-range"), attrs)
    return _xml_bytes(root)


def _multiget_body(report_name: str, data_prop: str, hrefs: Sequence[str]) -> bytes:
    root = ET.Element(report_name)
    _prop_request(root, data_prop)
    for href in hrefs:
        ET.SubElement(root, clark(NS_DAV, "href")).text = href
    return _xml_bytes(root)


def _prop_request(root: ET.Element, data_prop: str) -> None:
    prop = ET.SubElement(root, clark(NS_DAV, "prop"))
    ET.SubElement(prop, PROP_ETAG)
    ET.SubElement(prop, data_prop)


def _utc_text(moment: Union[_dt.datetime, _dt.date]) -> str:
    if not isinstance(moment, _dt.datetime):
        moment = _dt.datetime(moment.year, moment.month, moment.day)
    if moment.tzinfo is not None:
        moment = moment.astimezone(_dt.timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _items_with_data(resources: Tuple[DavResource, ...], data_prop: str, collection_href: str) -> Tuple[DavItem, ...]:
    own = _path_key(collection_href)
    return tuple(
        DavItem(r.href, r.prop(PROP_ETAG), r.prop(data_prop))
        for r in resources
        if _path_key(r.href) != own and not r.is_collection and (r.status is None or r.status < 300)
    )


def _path_key(href: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(href).path).rstrip("/")
