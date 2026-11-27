from datetime import date, datetime, timedelta, timezone

import pytest

from jornada.pim.tasks import UNKNOWN_COMPLETION_DATE
from jornada.sync.base import StoreError
from jornada.sync.tasks.common import (completion_date, date_of, html_to_text, is_real_completion, item_id_of,
                                       json_object, local_midnight_utc, parse_stamp, send, strings_of, text_of)
from jornada.webapi.http import HttpClient, HttpResponse, fake_transport, json_response

EASTERN = timezone(timedelta(hours=-4))
UTC = timezone.utc


def test_parse_stamp_reads_dates_date_times_fractions_and_offsets():
    assert parse_stamp("2026-09-10") == date(2026, 9, 10)
    assert parse_stamp("2026-09-10T04:00:00Z") == datetime(2026, 9, 10, 4, tzinfo=UTC)
    assert parse_stamp("2026-09-10T04:00:00.1234567Z") == datetime(2026, 9, 10, 4, 0, 0, 123456, tzinfo=UTC)
    assert parse_stamp("2026-09-10T04:00") == datetime(2026, 9, 10, 4)
    assert parse_stamp("2026-09-10 04:00:00-04:00") == datetime(2026, 9, 10, 4, tzinfo=EASTERN)
    assert parse_stamp("2026-09-10T04:00:00+0530") == datetime(2026, 9, 10, 4, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    for bad in ("2026-13-10", "2026-09-10T25:00:00Z", "yesterday", "", None, 42, "2026-09-10T04"):
        assert parse_stamp(bad) is None


def test_date_of_and_the_completion_helpers():
    assert date_of("2026-09-11T03:59:00Z", EASTERN) == date(2026, 9, 10)       # still the 10th in Eastern time
    assert date_of("2026-09-11", EASTERN) == date(2026, 9, 11) and date_of("nope", EASTERN) is None
    assert completion_date(False, "2026-09-08T04:00:00Z", EASTERN) is None
    assert completion_date(True, "2026-09-08T04:00:00Z", EASTERN) == date(2026, 9, 8)
    assert completion_date(True, "", EASTERN) == UNKNOWN_COMPLETION_DATE
    assert is_real_completion(date(2026, 9, 8))
    assert not is_real_completion(UNKNOWN_COMPLETION_DATE) and not is_real_completion(None)
    assert local_midnight_utc(date(2026, 9, 10), EASTERN) == datetime(2026, 9, 10, 4, tzinfo=UTC)
    assert local_midnight_utc(date(2026, 9, 10), UTC) == datetime(2026, 9, 10, 0, tzinfo=UTC)


def test_html_to_text_drops_tags_styles_and_scripts_but_keeps_line_breaks():
    html = ("<html><head><title>T</title><style>p {color: red}</style></head><body>"
            "<p>First &amp; foremost</p><div>Second<br>line</div><script>alert(1)</script>"
            "<ul><li>one</li><li>two</li></ul>\n\n\n<p>  spaced   out  </p></body></html>")
    assert html_to_text(html) == "First & foremost\nSecond\nline\none\ntwo\n\nspaced out"
    assert html_to_text("") == "" and html_to_text("plain") == "plain"


def test_http_helpers_turn_failures_into_store_errors():
    transport, _seen = fake_transport({
        ("GET", "/ok"): json_response(200, {"id": "x", "n": 1}),
        ("GET", "/list"): json_response(200, [1, 2]),
        ("GET", "/broken"): HttpResponse(200, (), b"not json"),
        ("GET", "/denied"): json_response(403, {"error": "no"}),
    })
    http = HttpClient("https://svc.example", transport=transport)
    payload = json_object(send(http, "Svc", "GET", "/ok"), "Svc", "reading")
    assert payload == {"id": "x", "n": 1} and item_id_of(payload, "Svc", "reading") == "x"
    with pytest.raises(StoreError) as info:
        send(http, "Svc", "GET", "/denied")
    assert str(info.value).startswith("Svc: ") and "403" in str(info.value)
    with pytest.raises(StoreError) as info:
        json_object(send(http, "Svc", "GET", "/denied", accept_errors=True), "Svc", "reading")
    assert "Svc reading failed: HTTP 403" in str(info.value)
    with pytest.raises(StoreError):
        json_object(send(http, "Svc", "GET", "/list"), "Svc", "reading")        # a list, not an object
    with pytest.raises(StoreError):
        json_object(send(http, "Svc", "GET", "/broken"), "Svc", "reading")      # not JSON at all
    with pytest.raises(StoreError):
        item_id_of({"id": ""}, "Svc", "create")


def test_text_helpers():
    assert text_of("a\r\nb") == "a\nb" and text_of(None) == "" and text_of(3) == ""
    assert strings_of(["a", 1, "b"]) == ("a", "b") and strings_of("ab") == () and strings_of(None) == ()
