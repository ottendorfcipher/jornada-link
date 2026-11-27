"""A small JSON-friendly HTTP client on ``urllib`` with an injectable transport.

Every backend talks to its service through :class:`HttpClient`, so tests can
substitute a transport function and no test ever touches the network.
Authorization headers never appear in error messages.
"""
from __future__ import annotations

import json as _json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple, Union

Headers = Tuple[Tuple[str, str], ...]
USER_AGENT = "jornada-link/0.1 (+https://github.com/ottendorfcipher/jornada-link)"
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_IDEMPOTENT = ("GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PROPFIND", "REPORT")
_SENSITIVE = ("authorization", "cookie", "x-api-key")
MAX_BODY = 64 * 1024 * 1024


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Headers = ()
    body: Optional[bytes] = None

    def header(self, name: str) -> Optional[str]:
        return _find_header(self.headers, name)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Headers = ()
    body: bytes = b""

    def header(self, name: str) -> Optional[str]:
        return _find_header(self.headers, name)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        if not self.body.strip():
            return None
        try:
            return _json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError(self.status, f"response is not JSON: {exc}", self.body) from exc


class HttpError(Exception):
    """A failed request: ``status`` (0 when the connection itself failed) and a short message."""

    def __init__(self, status: int, message: str, body: bytes = b"") -> None:
        excerpt = body[:300].decode("utf-8", errors="replace").replace("\n", " ") if body else ""
        super().__init__(f"HTTP {status}: {message}" + (f" — {excerpt}" if excerpt else ""))
        self.status = status
        self.message = message
        self.body = body


Transport = Callable[[HttpRequest], HttpResponse]


def _find_header(headers: Headers, name: str) -> Optional[str]:
    wanted = name.lower()
    for key, value in headers:
        if key.lower() == wanted:
            return value
    return None


def redact(headers: Iterable[Tuple[str, str]]) -> Headers:
    return tuple((k, "<redacted>" if k.lower() in _SENSITIVE else v) for k, v in headers)


def _origin(url: str) -> Tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but credentials never travel to a different origin."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(redirected.full_url):
            for name in ("Authorization", "Cookie", "X-api-key"):
                redirected.remove_header(name)
        return redirected


class _AnyMethodRequest(urllib.request.Request):
    def __init__(self, method: str, *args: Any, **kwargs: Any) -> None:
        self._method = method
        super().__init__(*args, **kwargs)

    def get_method(self) -> str:
        return self._method


def urllib_transport(timeout: float = 30.0) -> Transport:
    """The real transport; HTTP errors become responses, connection failures raise HttpError."""
    opener = urllib.request.build_opener(_SafeRedirectHandler)

    def bounded(stream: Any) -> bytes:
        body = stream.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise HttpError(0, f"response body exceeds {MAX_BODY} bytes")
        return body

    def send(request: HttpRequest) -> HttpResponse:
        req = _AnyMethodRequest(request.method, request.url, data=request.body,
                                headers={k: v for k, v in request.headers})
        try:
            with opener.open(req, timeout=timeout) as reply:
                return HttpResponse(reply.status, tuple(reply.headers.items()), bounded(reply))
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, tuple(exc.headers.items()), bounded(exc) or b"")
        except (urllib.error.URLError, OSError) as exc:
            raise HttpError(0, f"cannot reach {urllib.parse.urlsplit(request.url).netloc}: {exc}") from exc

    return send


def encode_query(params: Optional[Mapping[str, Any]]) -> str:
    if not params:
        return ""
    pairs = [(k, str(v)) for k, v in params.items() if v is not None]
    return urllib.parse.urlencode(pairs, doseq=False, quote_via=urllib.parse.quote)


class HttpClient:
    def __init__(self, base_url: str = "", headers: Iterable[Tuple[str, str]] = (), timeout: float = 30.0,
                 transport: Optional[Transport] = None, retries: int = 2,
                 sleep: Callable[[float], None] = time.sleep,
                 auth: Optional[Callable[[], Optional[str]]] = None) -> None:
        self._base = base_url.rstrip("/")
        self._headers = tuple(headers)
        self._transport = transport or urllib_transport(timeout)
        self._retries = max(retries, 0)
        self._sleep = sleep
        self._auth = auth

    def url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        full = path if path.startswith(("http://", "https://")) else self._base + "/" + path.lstrip("/")
        query = encode_query(params)
        if query:
            full += ("&" if "?" in full else "?") + query
        return full

    def request(self, method: str, path: str, params: Optional[Mapping[str, Any]] = None,
                headers: Iterable[Tuple[str, str]] = (), body: Optional[bytes] = None,
                json_body: Any = None, form: Optional[Mapping[str, Any]] = None,
                accept_errors: bool = False) -> HttpResponse:
        merged = [("User-Agent", USER_AGENT), *self._headers, *headers]
        if json_body is not None:
            body = _json.dumps(json_body).encode("utf-8")
            merged.append(("Content-Type", "application/json"))
        elif form is not None:
            body = encode_query(form).encode("ascii")
            merged.append(("Content-Type", "application/x-www-form-urlencoded"))
        token = self._auth() if self._auth is not None else None
        if token:
            merged.append(("Authorization", token))
        request = HttpRequest(method.upper(), self.url(path, params), tuple(merged), body)
        response = self._send_with_retries(request)
        if not response.ok and not accept_errors:
            raise HttpError(response.status, f"{method.upper()} {_safe_url(request.url)} failed", response.body)
        return response

    def _send_with_retries(self, request: HttpRequest) -> HttpResponse:
        attempt = 0
        while True:
            response = self._transport(request)
            if not _retryable(request.method, response.status) or attempt >= self._retries:
                return response
            attempt += 1
            retry_after = response.header("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * attempt
            self._sleep(min(delay, 30.0))

    def get(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("DELETE", path, **kwargs)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.get(path, **kwargs).json()


def _retryable(method: str, status: int) -> bool:
    """429 is always safe to retry; 5xx only for methods a server may have applied at most once."""
    if status == 429:
        return True
    return status in _RETRY_STATUSES and method.upper() in _IDEMPOTENT


def _safe_url(url: str) -> str:
    """The URL without its query string (queries can carry tokens)."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def bearer(token: str) -> str:
    return f"Bearer {token}"


def basic(username: str, password: str) -> str:
    import base64
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def fake_transport(routes: Dict[Tuple[str, str], Union[HttpResponse, Callable[[HttpRequest], HttpResponse]]],
                   default: Optional[HttpResponse] = None) -> Tuple[Transport, list]:
    """For tests: route ``(METHOD, path)`` → response or handler; records every request."""
    seen: list = []

    def send(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        path = urllib.parse.urlsplit(request.url).path
        handler = routes.get((request.method, path))
        if handler is None:
            if default is not None:
                return default
            return HttpResponse(404, (), b'{"error":"no route"}')
        return handler(request) if callable(handler) else handler

    return send, seen


def json_response(status: int, payload: Any, headers: Headers = ()) -> HttpResponse:
    return HttpResponse(status, (("Content-Type", "application/json"),) + tuple(headers),
                        _json.dumps(payload).encode("utf-8"))
