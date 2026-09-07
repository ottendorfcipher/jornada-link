import json

import pytest

from jornada.webapi.http import (HttpClient, HttpError, HttpRequest, HttpResponse, basic, bearer, encode_query,
                                 fake_transport, json_response, redact, urllib_transport)


def test_get_json_with_params_and_auth():
    transport, seen = fake_transport({("GET", "/v1/items"): json_response(200, {"items": [1, 2]})})
    client = HttpClient("https://api.example.com", transport=transport, auth=lambda: bearer("tok"))
    assert client.get_json("/v1/items", params={"q": "a b", "n": 2, "skip": None}) == {"items": [1, 2]}
    request = seen[0]
    assert request.url == "https://api.example.com/v1/items?q=a%20b&n=2"
    assert request.header("Authorization") == "Bearer tok" and request.header("user-agent").startswith("jornada-link")
    assert dict(redact(request.headers))["Authorization"] == "<redacted>"


def test_post_json_and_form_bodies():
    transport, seen = fake_transport({("POST", "/j"): json_response(201, {"ok": True}), ("POST", "/f"): HttpResponse(204)})
    client = HttpClient("https://x", transport=transport)
    assert client.post("/j", json_body={"a": 1}).json() == {"ok": True}
    assert json.loads(seen[0].body) == {"a": 1} and seen[0].header("Content-Type") == "application/json"
    assert client.post("/f", form={"grant_type": "x", "code": "a b"}).status == 204
    assert seen[1].body == b"grant_type=x&code=a%20b"
    assert client.request("GET", "https://other.example/abs", accept_errors=True).status == 404


def test_errors_carry_status_and_no_query_string():
    transport, _ = fake_transport({("GET", "/secret"): HttpResponse(403, (), b'{"error":"denied"}')})
    client = HttpClient("https://x", transport=transport)
    with pytest.raises(HttpError) as info:
        client.get("/secret", params={"access_token": "abc"})
    assert info.value.status == 403 and "abc" not in str(info.value) and "denied" in str(info.value)
    assert client.get("/secret", params={"t": 1}, accept_errors=True).status == 403
    with pytest.raises(HttpError):
        HttpResponse(200, (), b"not json").json()
    assert HttpResponse(200, (), b"  ").json() is None


def test_retries_on_429_and_5xx_with_sleep():
    attempts = []
    def flaky(request: HttpRequest) -> HttpResponse:
        attempts.append(request.method)
        if len(attempts) == 1:
            return HttpResponse(503, (), b"")
        if len(attempts) == 2:
            return HttpResponse(429, (("Retry-After", "2"),), b"")
        return json_response(200, {"done": True})
    slept = []
    client = HttpClient("https://x", transport=flaky, retries=3, sleep=slept.append)
    assert client.get_json("/") == {"done": True}
    assert len(attempts) == 3 and slept == [1.5, 2.0]
    gave_up = HttpClient("https://x", transport=lambda r: HttpResponse(500), retries=1, sleep=lambda _s: None)
    with pytest.raises(HttpError):
        gave_up.get("/")


def test_helpers():
    assert basic("u", "p") == "Basic dTpw"
    assert encode_query({"a": "x/y", "b": 1}) == "a=x%2Fy&b=1" and encode_query(None) == ""
    assert HttpResponse(200, (("X-A", "1"),)).header("x-a") == "1" and HttpResponse(200).header("nope") is None
    send = urllib_transport(timeout=1)
    with pytest.raises(HttpError) as info:
        send(HttpRequest("GET", "http://127.0.0.1:9/nothing"))
    assert info.value.status == 0
