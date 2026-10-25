import time
from datetime import datetime, timedelta, timezone

import pytest

from jornada.sync.base import StoreError
from jornada.sync.calendar.common import (all_day_end, excerpt, iso_offset, json_call, json_object, local_aware,
                                          local_wall_clock, local_zone_name, nearest_midnight, send, string_field)
from jornada.webapi.http import HttpClient, HttpError, HttpResponse, fake_transport, json_response


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_local_conversions_follow_dst_for_the_moment_itself(berlin):
    assert local_aware(datetime(2026, 1, 15, 9)).utcoffset() == timedelta(hours=1)
    assert local_aware(datetime(2026, 7, 15, 9)).utcoffset() == timedelta(hours=2)
    assert iso_offset(datetime(2026, 7, 15, 9, 0, 0, 500)) == "2026-07-15T09:00:00+02:00"
    assert iso_offset(datetime(2026, 1, 15, 9)) == "2026-01-15T09:00:00+01:00"
    assert local_wall_clock(datetime(2026, 7, 15, 7, tzinfo=timezone.utc)) == datetime(2026, 7, 15, 9)
    assert local_wall_clock(datetime(2026, 1, 15, 8, tzinfo=timezone.utc)) == datetime(2026, 1, 15, 9)
    floating = datetime(2026, 1, 15, 9)
    assert local_wall_clock(floating) is floating
    aware = local_aware(floating)
    assert local_aware(aware) is aware and local_wall_clock(aware) == floating
    assert local_zone_name() == "Europe/Berlin"


def test_the_edges_of_the_datetime_range_fall_back_instead_of_failing(berlin):
    assert local_wall_clock(datetime(9999, 12, 31, 23, tzinfo=timezone.utc)) == datetime(9999, 12, 31, 23)   # kept floating
    assert local_wall_clock(datetime(1, 1, 1, tzinfo=timezone.utc)).year == 1                                 # still representable
    assert local_aware(datetime(1, 1, 1)).tzinfo is not None and local_aware(datetime(9999, 12, 31, 23)).year == 9999
    assert iso_offset(datetime(1, 1, 1)).startswith("0001-01-01T00:00:00+")


def test_local_zone_name_sources(tmp_path):
    link = tmp_path / "localtime"
    link.symlink_to("/var/db/timezone/zoneinfo/Europe/Paris")
    assert local_zone_name({}, str(link)) == "Europe/Paris"
    assert local_zone_name({"TZ": "America/New_York"}, str(link)) == "America/New_York"
    assert local_zone_name({"TZ": "CET-1CEST"}, str(link)) == "Europe/Paris"      # a POSIX rule is not a name
    assert local_zone_name({"TZ": ":Asia/Tokyo"}, str(tmp_path / "missing")) == "Asia/Tokyo"
    assert local_zone_name({}, str(tmp_path / "missing")) is None
    (tmp_path / "plain").write_text("x")
    assert local_zone_name({}, str(tmp_path / "plain")) is None


def test_all_day_helpers():
    assert all_day_end(datetime(2026, 3, 13), datetime(2026, 3, 15)) == datetime(2026, 3, 15)
    assert all_day_end(datetime(2026, 3, 13, 8), datetime(2026, 3, 14, 23, 59)) == datetime(2026, 3, 15)
    assert all_day_end(datetime(2026, 3, 13), datetime(2026, 3, 13)) == datetime(2026, 3, 14)
    assert all_day_end(datetime(2026, 3, 13), datetime(2026, 3, 1)) == datetime(2026, 3, 14)
    assert nearest_midnight(datetime(2026, 3, 13, 1)) == datetime(2026, 3, 13)
    assert nearest_midnight(datetime(2026, 3, 12, 19)) == datetime(2026, 3, 13)
    assert nearest_midnight(datetime(2026, 3, 13)) == datetime(2026, 3, 13)


def test_http_helpers_turn_failures_into_store_errors():
    transport, _ = fake_transport({
        ("GET", "/ok"): json_response(200, {"id": "1"}),
        ("GET", "/empty"): HttpResponse(204),
        ("GET", "/bad"): HttpResponse(500, (), b"<html>  Server \n  exploded </html>"),
        ("GET", "/text"): HttpResponse(200, (), b"not json"),
    })
    http = HttpClient("https://api.example", transport=transport, retries=0)
    assert json_call(http, "Svc", "GET", "/ok", "read") == {"id": "1"}
    assert json_call(http, "Svc", "GET", "/empty", "read") is None
    with pytest.raises(StoreError) as info:
        json_call(http, "Svc", "GET", "/bad", "read the thing")
    assert str(info.value) == "Svc: could not read the thing: HTTP 500 (<html> Server exploded </html>)"
    with pytest.raises(StoreError):
        json_call(http, "Svc", "GET", "/text", "read")
    assert excerpt(HttpResponse(400)) == "" and excerpt(HttpResponse(400, (), b"x" * 500), limit=3) == " (xxx)"

    def down(request):
        raise HttpError(0, "cannot reach api.example: refused")

    with pytest.raises(StoreError) as info:
        send(HttpClient("https://api.example", transport=down, retries=0), "Svc", "GET", "/x", "ping")
    assert "cannot reach api.example" in str(info.value)
    assert json_object({"a": 1}, "Svc", "read") == {"a": 1} and string_field({"id": "a"}, "Svc", "create") == "a"
    with pytest.raises(StoreError):
        json_object([], "Svc", "read")
    with pytest.raises(StoreError):
        string_field({"id": ""}, "Svc", "create")
    with pytest.raises(StoreError):
        string_field({"id": 5}, "Svc", "create")
