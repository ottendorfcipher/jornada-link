import base64
import datetime as dt

import pytest

from jornada.webapi import dav, ical
from tests.fake_dav import DELETED_HREF, EVENT_A, PASSWORD, SYNC_TOKEN, USERNAME, FakeDavServer

UTC = dt.timezone.utc
BASIC = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()


@pytest.fixture
def server():
    srv = FakeDavServer().start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def client(server):
    return dav.DavClient(server.base_url, USERNAME, PASSWORD, timeout=5)


def requests_for(server, method):
    return [(path, headers) for verb, path, headers in server.requests if verb == method]


# --- against the fake server ------------------------------------------------------------------

def test_options_reports_dav_capabilities(client):
    caps = client.options()
    assert {"1", "3", "calendar-access", "addressbook"} <= set(caps)


def test_unauthorized_error_never_leaks_credentials(server):
    bad = dav.DavClient(server.base_url, USERNAME, "wrong-" + PASSWORD, timeout=5)
    with pytest.raises(dav.DavError) as info:
        bad.propfind("/", (dav.PROP_CURRENT_USER_PRINCIPAL,))
    assert info.value.status == 401
    assert PASSWORD not in info.value.message and "wrong-" not in info.value.message
    assert PASSWORD not in repr(bad)


def test_discover_principal_direct_and_via_well_known_redirect(server, client):
    assert dav.discover_principal(client) == server.base_url + "principals/u/"
    elsewhere = dav.DavClient(server.base_url + "nowhere/", USERNAME, PASSWORD, timeout=5)
    server.requests.clear()
    assert dav.discover_principal(elsewhere) == server.base_url + "principals/u/"
    paths = [path for path, _ in requests_for(server, "PROPFIND")]
    assert paths == ["/nowhere/", "/.well-known/caldav", "/"]


def test_discover_calendars(server, client):
    calendars = dav.discover_calendars(client)
    assert [c.display_name for c in calendars] == ["Work", "Tasks"]
    work, tasks = calendars
    assert work.href == server.base_url + "cal/work/"
    assert (work.ctag, work.components, work.kind) == ("ctag-work-7", ("VEVENT", "VTODO"), "calendar")
    assert tasks.components == ("VTODO",)
    depths = [headers.get("Depth") for path, headers in requests_for(server, "PROPFIND") if path == "/cal/"]
    assert depths == ["1"]


def test_discover_addressbooks(server, client):
    (book,) = dav.discover_addressbooks(client)
    assert book == dav.Collection(server.base_url + "card/default/", "Contacts", "ctag-card-3", (), "addressbook")


def test_discovery_falls_back_to_base_url_collection(server):
    server.principal_enabled = False
    direct = dav.DavClient(server.base_url + "cal/work/", USERNAME, PASSWORD, timeout=5)
    (calendar,) = dav.discover_calendars(direct)
    assert calendar.href == server.base_url + "cal/work/"
    assert calendar.display_name == "Self"
    with pytest.raises(dav.DavError):
        dav.discover_calendars(dav.DavClient(server.base_url + "cal/nothing/", USERNAME, PASSWORD, timeout=5))
    with pytest.raises(dav.DavError):
        dav.discover_principal(direct)


def test_list_items_excludes_collection_itself_and_subcollections(server, client):
    items = dav.list_items(client, server.base_url + "cal/work/")
    assert items == (
        dav.DavItem("/cal/work/a.ics", '"etag-a-1"', None),
        dav.DavItem("/cal/work/b.ics", '"etag-b-1"', None),
    )


def test_calendar_query_with_and_without_time_range(server, client):
    everything = dav.calendar_query(client, "/cal/work/")
    assert [i.href for i in everything] == ["/cal/work/a.ics", "/cal/work/b.ics"]
    assert all(i.data.startswith("BEGIN:VCALENDAR") and i.etag for i in everything)
    march = dav.calendar_query(client, "/cal/work/", start=dt.datetime(2026, 3, 1, tzinfo=UTC), end=dt.datetime(2026, 4, 1, tzinfo=UTC))
    assert [i.href for i in march] == ["/cal/work/a.ics"]
    assert ical.parse(march[0].data) == ical.parse(EVENT_A)  # XML normalises CRLF to LF
    _, headers = requests_for(server, "REPORT")[-1]
    assert headers.get("Depth") == "1"
    assert headers.get("Content-Type", "").startswith("application/xml")
    later = dav.calendar_query(client, "/cal/work/", start=dt.datetime(2026, 4, 1, 12, 0))  # naive = UTC
    assert [i.href for i in later] == ["/cal/work/b.ics"]


def test_calendar_multiget(server, client):
    items = dav.calendar_multiget(client, "/cal/work/", ["/cal/work/b.ics", "/cal/work/missing.ics"])
    assert [i.href for i in items] == ["/cal/work/b.ics"]
    assert "April meeting" in items[0].data
    server.requests.clear()
    assert dav.calendar_multiget(client, "/cal/work/", []) == ()
    assert server.requests == []


def test_addressbook_query_and_multiget(client):
    (card,) = dav.addressbook_query(client, "/card/default/")
    assert card.href == "/card/default/c1.vcf" and card.etag == '"etag-c1"'
    assert "FN:Jane Doe" in card.data
    assert dav.addressbook_multiget(client, "/card/default/", ["/card/default/c1.vcf"]) == (card,)
    assert dav.addressbook_multiget(client, "/card/default/", []) == ()


def test_get_put_delete_lifecycle(server, client):
    etag, body = client.get("/cal/work/a.ics")
    assert (etag, body) == ('"etag-a-1"', EVENT_A.encode())
    created = client.put("/cal/work/new.ics", EVENT_A.encode(), "text/calendar; charset=utf-8", if_none_match="*")
    assert created == '"etag-2"'
    with pytest.raises(dav.DavConflict) as conflict:
        client.put("/cal/work/new.ics", b"x", "text/calendar", if_match='"stale"')
    assert conflict.value.status == 412
    with pytest.raises(dav.DavConflict):
        client.put("/cal/work/new.ics", b"x", "text/calendar", if_none_match="*")
    updated = client.put("/cal/work/new.ics", EVENT_A.encode(), "text/calendar", if_match="etag-2")  # quoted for us
    assert updated == '"etag-3"'
    assert server.items["/cal/work/new.ics"].etag == '"etag-3"'
    with pytest.raises(dav.DavConflict):
        client.delete("/cal/work/new.ics", if_match='"etag-2"')
    client.delete("/cal/work/new.ics", if_match='"etag-3"')
    with pytest.raises(dav.DavNotFound):
        client.get("/cal/work/new.ics")
    with pytest.raises(dav.DavNotFound) as missing:
        client.delete("/cal/work/new.ics")
    assert missing.value.status == 404


def test_sync_collection(server, client):
    changed, deleted, token = dav.sync_collection(client, "/cal/work/", None)
    assert changed == (dav.DavItem("/cal/work/a.ics", '"etag-a-1"', None), dav.DavItem("/cal/work/b.ics", '"etag-b-1"', None))
    assert deleted == (DELETED_HREF,)
    assert token == SYNC_TOKEN
    _, headers = requests_for(server, "REPORT")[-1]
    assert "Depth" not in headers
    assert dav.sync_collection(client, "/cal/work/", token)[2] == SYNC_TOKEN


def test_sync_collection_forbidden_falls_back_to_listing(server, client):
    server.sync_enabled = False
    with pytest.raises(dav.DavError) as info:
        dav.sync_collection(client, "/cal/work/", "stale-token")
    assert info.value.status == 403
    assert "valid-sync-token" in info.value.message
    fallback = dav.list_items(client, "/cal/work/")
    assert len(fallback) == 2


# --- with an injected transport ---------------------------------------------------------------

MULTISTATUS = b"""<?xml version="1.0"?>
<multistatus xmlns="DAV:">
  <response>
    <href>/dav/cal/work/1.ics</href>
    <propstat>
      <prop><getetag>"one"</getetag></prop>
    </propstat>
    <propstat>
      <prop><displayname/></prop>
      <status>HTTP/1.1 404 Not Found</status>
    </propstat>
  </response>
  <response>
    <href>/dav/cal/work/gone.ics</href>
    <status>HTTP/1.1 404 Not Found</status>
  </response>
  <sync-token>token-9</sync-token>
</multistatus>"""


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def test_basic_auth_method_depth_and_body():
    recorder = Recorder(dav.Response(207, (("Content-Type", "application/xml"),), MULTISTATUS))
    client = dav.DavClient("https://dav.example/dav", USERNAME, PASSWORD, transport=recorder)
    assert client.base_url == "https://dav.example/dav/"
    resources = client.propfind("cal/work/", (dav.PROP_ETAG, dav.PROP_DISPLAYNAME), depth=1)
    (request,) = recorder.requests
    assert (request.method, request.url) == ("PROPFIND", "https://dav.example/dav/cal/work/")
    assert request.header("authorization") == BASIC
    assert request.header("Depth") == "1"
    assert request.header("Content-Type") == dav.CONTENT_TYPE_XML
    assert request.header("User-Agent") == dav.USER_AGENT
    assert request.body.startswith(b"<?xml") and b'xmlns:D="DAV:"' in request.body and b"<D:getetag />" in request.body
    live, gone = resources
    assert (live.href, live.status, dict(live.props)) == ("/dav/cal/work/1.ics", 200, {dav.PROP_ETAG: '"one"'})
    assert (gone.href, gone.status, dict(gone.props)) == ("/dav/cal/work/gone.ics", 404, {})


def test_bearer_token_and_no_credentials():
    recorder = Recorder(dav.Response(200, (("DAV", "1, 3"),)))
    dav.DavClient("https://h/", bearer_token="tok-123", transport=recorder).options()
    assert recorder.requests[0].header("Authorization") == "Bearer tok-123"
    anonymous = Recorder(dav.Response(200, (("DAV", "1"),)))
    assert dav.DavClient("http://h/", transport=anonymous).options() == ("1",)
    assert anonymous.requests[0].header("Authorization") is None


def test_credentials_in_url_are_used_but_never_shown():
    recorder = Recorder(dav.Response(500, (), b"<html><body>Server exploded: hunter2</body></html>"))
    client = dav.DavClient("https://bob:hunter2@h.example/dav", transport=recorder)
    assert client.base_url == "https://h.example/dav/"
    with pytest.raises(dav.DavError) as info:
        client.propfind("/", (dav.PROP_ETAG,))
    assert recorder.requests[0].header("Authorization") == "Basic " + base64.b64encode(b"bob:hunter2").decode()
    assert "hunter2@" not in info.value.message and "bob:" not in info.value.message
    assert "Server exploded" in info.value.message and "<html>" not in info.value.message
    assert dav.safe_url("https://bob:hunter2@h.example:8443/x?y=1") == "https://h.example:8443/x?y=1"


@pytest.mark.parametrize("status,error", [(404, dav.DavNotFound), (412, dav.DavConflict), (500, dav.DavError), (200, dav.DavError)])
def test_status_mapping(status, error):
    client = dav.DavClient("https://h/", transport=Recorder(dav.Response(status)))
    with pytest.raises(error) as info:
        client.propfind("/x", (dav.PROP_ETAG,))
    assert info.value.status == status
    assert issubclass(error, dav.DavError)


def test_redirects_are_followed_but_never_downgraded():
    recorder = Recorder(
        dav.Response(301, (("Location", "/moved/"),)),
        dav.Response(207, (), MULTISTATUS),
    )
    client = dav.DavClient("https://h/", transport=recorder)
    final_url, _ = client.propfind_at("/old/", (dav.PROP_ETAG,))
    assert [r.url for r in recorder.requests] == ["https://h/old/", "https://h/moved/"]
    assert final_url == "https://h/moved/"
    downgrade = dav.DavClient("https://h/", transport=Recorder(dav.Response(302, (("Location", "http://h/insecure/"),))))
    with pytest.raises(dav.DavError):
        downgrade.propfind("/", (dav.PROP_ETAG,))
    looping = dav.DavClient("https://h/", transport=Recorder(dav.Response(307, (("Location", "/again/"),))))
    with pytest.raises(dav.DavError) as info:
        looping.propfind("/", (dav.PROP_ETAG,))
    assert "redirects" in info.value.message
    unsafe = Recorder(dav.Response(302, (("Location", "/elsewhere/"),)))
    with pytest.raises(dav.DavError) as info:
        dav.DavClient("https://h/", transport=unsafe).put("/x.ics", b"x", "text/calendar")
    assert info.value.status == 302 and len(unsafe.requests) == 1


def test_parse_multistatus_edge_cases():
    parsed = dav.parse_multistatus(MULTISTATUS)
    assert parsed.sync_token == "token-9"
    assert parsed.responses[0].prop(dav.PROP_ETAG) == '"one"'
    assert parsed.responses[0].prop(dav.PROP_DISPLAYNAME) is None
    assert not parsed.responses[0].is_collection
    with pytest.raises(dav.DavError):
        dav.parse_multistatus(b"<multistatus xmlns='DAV:'><response>")
    resource = dav.DavResource("/x", None, {dav.PROP_RESOURCETYPE: f"{dav.TYPE_COLLECTION},{dav.TYPE_CALENDAR}"})
    assert resource.is_collection and resource.has_type(dav.TYPE_CALENDAR)
    assert resource.resource_types == (dav.TYPE_COLLECTION, dav.TYPE_CALENDAR)
    with pytest.raises(TypeError):
        resource.props["x"] = "y"  # type: ignore[index]


def test_sync_collection_requires_a_token_and_skips_collection_itself():
    body = b"""<D:multistatus xmlns:D="DAV:"><D:response><D:href>/c/</D:href><D:propstat><D:prop>
      <D:getetag>"c"</D:getetag></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
      <D:response><D:href>/c/1.ics</D:href><D:propstat><D:prop><D:getetag>"1"</D:getetag></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>"""
    client = dav.DavClient("https://h/", transport=Recorder(dav.Response(207, (), body)))
    with pytest.raises(dav.DavError) as info:
        dav.sync_collection(client, "/c/", None)
    assert "sync token" in info.value.message
    with_token = body.replace(b"</D:multistatus>", b"<D:sync-token>t2</D:sync-token></D:multistatus>")
    client = dav.DavClient("https://h/", transport=Recorder(dav.Response(207, (), with_token)))
    changed, deleted, token = dav.sync_collection(client, "/c/", "t1")
    assert (changed, deleted, token) == ((dav.DavItem("/c/1.ics", '"1"', None),), (), "t2")


def test_input_validation():
    with pytest.raises(dav.DavError):
        dav.DavClient("ftp://h/")
    with pytest.raises(dav.DavError):
        dav.DavClient("not a url")
    client = dav.DavClient("https://h/", transport=Recorder(dav.Response(207, (), MULTISTATUS)))
    with pytest.raises(dav.DavError):
        client.resolve("file:///etc/passwd")
    with pytest.raises(dav.DavError):
        client.propfind("/", ("getetag",))
    with pytest.raises(dav.DavError):
        dav.calendar_query(client, "/", component="VEVENT; DROP")
    assert client.resolve("https://other.example/x/") == "https://other.example/x/"
    assert client.resolve("/abs/") == "https://h/abs/"


def test_etag_quoting_on_put_and_delete():
    recorder = Recorder(dav.Response(204, (("ETag", '"new"'),)))
    client = dav.DavClient("https://h/", transport=recorder)
    assert client.put("/x.ics", b"data", "text/calendar", if_match="abc") == '"new"'
    assert recorder.requests[-1].header("If-Match") == '"abc"'
    client.put("/x.ics", b"data", "text/calendar", if_match='W/"weak"', if_none_match="*")
    assert recorder.requests[-1].header("If-Match") == 'W/"weak"'
    assert recorder.requests[-1].header("If-None-Match") == "*"
    client.delete("/x.ics", if_match='"q"')
    assert (recorder.requests[-1].method, recorder.requests[-1].header("If-Match")) == ("DELETE", '"q"')


def test_calendar_query_body_shape():
    recorder = Recorder(dav.Response(207, (), b'<D:multistatus xmlns:D="DAV:"/>'))
    client = dav.DavClient("https://h/", transport=recorder)
    assert dav.calendar_query(client, "/c/", "VTODO", start=dt.date(2026, 1, 1)) == ()
    body = recorder.requests[0].body.decode()
    assert 'comp-filter name="VCALENDAR"' in body and 'comp-filter name="VTODO"' in body
    assert 'time-range start="20260101T000000Z"' in body and "end=" not in body
    assert "calendar-data" in body and "getetag" in body


def test_response_and_request_helpers():
    response = dav.Response(200, (("ETag", "a"), ("DAV", "1"), ("dav", "2")))
    assert response.header("etag") == "a" and response.header("nope") is None
    assert response.header_values("DAV") == ("1", "2")
    assert dav.Request("GET", "https://h/", (("X", "y"),)).header("x") == "y"


def test_urllib_transport_reports_connection_failures():
    transport = dav.urllib_transport(timeout=2.0)
    with pytest.raises(dav.DavError) as info:
        transport(dav.Request("GET", "http://127.0.0.1:1/"))
    assert info.value.status is None
    assert "127.0.0.1" in info.value.message


def test_ipv6_hosts_and_response_size_limit(server, client, monkeypatch):
    six = dav.DavClient("http://user:pw@[::1]:8080/dav", transport=Recorder(dav.Response(200, (("DAV", "1"),))))
    assert six.base_url == "http://[::1]:8080/dav/"
    assert dav.safe_url("http://user:pw@[::1]:8080/dav") == "http://[::1]:8080/dav"
    monkeypatch.setattr(dav, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(dav.DavError) as info:
        client.get("/cal/work/a.ics")
    assert "larger than" in info.value.message


def test_redirect_to_unsupported_scheme_is_refused():
    client = dav.DavClient("https://h/", transport=Recorder(dav.Response(301, (("Location", "ftp://h/x"),))))
    with pytest.raises(dav.DavError) as info:
        client.propfind("/", (dav.PROP_ETAG,))
    assert "unsupported" in info.value.message


def multistatus(*responses):
    body = "".join(responses)
    return f'<D:multistatus xmlns:D="DAV:" xmlns:C="{dav.NS_CALDAV}">{body}</D:multistatus>'.encode()


def ok(href, props):
    return f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>{props}</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"


def test_discovery_with_empty_home_and_unnamed_calendar():
    principal = ok("/", "<D:current-user-principal><D:href>/p/</D:href></D:current-user-principal>")
    home = ok("/p/", "<C:calendar-home-set><D:href>/home/</D:href></C:calendar-home-set>")
    plain = ok("/home/", "<D:resourcetype><D:collection/></D:resourcetype>")
    unnamed = ok("/home/personal-2/", "<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>")
    listing = Recorder(
        dav.Response(207, (), multistatus(principal)), dav.Response(207, (), multistatus(home)),
        dav.Response(207, (), multistatus(plain, unnamed)),
    )
    (calendar,) = dav.discover_calendars(dav.DavClient("https://h/", transport=listing))
    assert (calendar.href, calendar.display_name, calendar.ctag, calendar.components) == ("https://h/home/personal-2/", "personal-2", None, ())
    empty = Recorder(
        dav.Response(207, (), multistatus(principal)), dav.Response(207, (), multistatus(home)),
        dav.Response(207, (), multistatus(plain)), dav.Response(207, (), multistatus(ok("/", "<D:resourcetype><D:collection/></D:resourcetype>"))),
    )
    assert dav.discover_calendars(dav.DavClient("https://h/", transport=empty)) == ()
    assert [r.method for r in empty.requests] == ["PROPFIND"] * 4
