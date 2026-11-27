"""The bridge process: one shared backend behind a lock, the POP3 and SMTP servers on their
own threads, and ``run_bridge`` — what `jornada sync run` calls for a mail account."""
from __future__ import annotations

import errno
import threading
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from ..accounts import Account, AccountError
from ..registry import BuildContext
from .backend import MailBackend, MailError, MailSummary
from .config import (DEFAULT_POP3_PORT, DEFAULT_SMTP_PORT, UNPRIVILEGED_POP3_PORT, UNPRIVILEGED_SMTP_PORT,
                     BridgeConfig, config_from_account)
from .pop3 import Pop3Server
from .smtp import SmtpServer

Log = Callable[[str], None]
POLL_SECONDS = 0.5
THREAD_JOIN_SECONDS = 2.0
BACKEND_HOOK = "mail_backend"
START_HOOK = "mail_on_start"


def silent(_line: str) -> None:
    return


class SharedBackend:
    """One backend for every connection: calls are serialised, the connection opens on first use
    and reopens after a failure (servers drop idle IMAP sessions; the next call recovers)."""

    def __init__(self, backend: MailBackend, log: Log = silent) -> None:
        self._backend = backend
        self._log = log
        self._lock = threading.RLock()
        self._connected = False
        self.name = backend.name

    def connect(self) -> None:
        with self._lock:
            self._ensure_connected()

    def list(self) -> Tuple[MailSummary, ...]:
        return self._call("list")

    def fetch(self, uid: str) -> bytes:
        return self._call("fetch", uid)

    def fetch_headers(self, uid: str) -> bytes:
        return self._call("fetch_headers", uid)

    def delete(self, uids: Sequence[str]) -> None:
        self._call("delete", tuple(uids))

    def send(self, raw: bytes, sender: str, recipients: Sequence[str]) -> None:
        self._call("send", raw, sender, tuple(recipients), retry=False)

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _ensure_connected(self) -> None:
        if not self._connected:
            self._backend.connect()
            self._connected = True

    def _drop(self) -> None:
        if not self._connected:
            return
        self._connected = False
        try:
            self._backend.close()
        except (MailError, OSError) as exc:
            self._log(f"{self.name}: closing the connection failed: {exc}")

    def _call(self, method: str, *args: Any, retry: bool = True) -> Any:
        with self._lock:
            was_connected = self._connected
            try:
                return self._invoke(method, *args)
            except MailError as exc:
                if not (retry and was_connected):
                    raise
                self._log(f"{self.name}: {exc}; reconnecting")
                return self._invoke(method, *args)

    def _invoke(self, method: str, *args: Any) -> Any:
        try:
            self._ensure_connected()
            return getattr(self._backend, method)(*args)
        except (MailError, OSError) as exc:
            self._drop()
            raise MailError(str(exc)) from exc


def _serve(server: Any, name: str) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name=f"mail-{name}", daemon=True)
    thread.start()
    return thread


class MailBridge:
    """The POP3 and SMTP servers of one mail account, sharing one backend."""

    def __init__(self, backend: MailBackend, config: BridgeConfig, log: Log = silent) -> None:
        self.backend = backend if isinstance(backend, SharedBackend) else SharedBackend(backend, log)
        self.config = config
        self.log = log
        self._pop3: Optional[Pop3Server] = None
        self._smtp: Optional[SmtpServer] = None
        self._threads: Tuple[threading.Thread, ...] = ()
        self._stopped = threading.Event()

    @property
    def pop3_port(self) -> int:
        return self._pop3.port if self._pop3 is not None else self.config.pop3_port

    @property
    def smtp_port(self) -> int:
        return self._smtp.port if self._smtp is not None else self.config.smtp_port

    def start(self) -> None:
        """Bind both servers (raises OSError with the first that fails) and serve them on threads."""
        if self._pop3 is not None:
            raise RuntimeError("the mail bridge is already running")
        pop3 = Pop3Server((self.config.listen, self.config.pop3_port), self.config, self.backend, self.log)
        try:
            smtp = SmtpServer((self.config.listen, self.config.smtp_port), self.config, self.backend, self.log)
        except OSError:
            pop3.server_close()
            raise
        self._pop3, self._smtp = pop3, smtp
        self._stopped.clear()
        self._threads = (_serve(pop3, "pop3"), _serve(smtp, "smtp"))

    def serve_forever(self) -> None:
        """Block until :meth:`stop` is called from another thread (or Ctrl-C interrupts the wait)."""
        while not self._stopped.wait(POLL_SECONDS):
            pass

    def stop(self) -> None:
        self._stopped.set()
        for server in (self._pop3, self._smtp):
            if server is not None:
                server.shutdown()
                server.server_close()
        for thread in self._threads:
            thread.join(THREAD_JOIN_SECONDS)
        self._pop3, self._smtp, self._threads = None, None, ()
        self.backend.close()


def bind_error(config: BridgeConfig, exc: OSError) -> str:
    where = f"{config.listen} (POP3 port {config.pop3_port}, SMTP port {config.smtp_port})"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (f"error: cannot listen on {where}: permission denied. Ports below 1024 need root — run with "
                f"sudo, or use --set pop3_port={UNPRIVILEGED_POP3_PORT} --set smtp_port={UNPRIVILEGED_SMTP_PORT} "
                "and set the same ports in the device's Inbox service")
    if exc.errno == errno.EADDRINUSE:
        return f"error: cannot listen on {where}: the port is already in use (is another bridge running?)"
    if exc.errno == errno.EADDRNOTAVAIL:
        return (f"error: cannot listen on {where}: this Mac has no such address — is the PPP link up? "
                "(--set listen=IP overrides the address)")
    return f"error: cannot listen on {where}: {exc.strerror or exc}"


def instructions(account: Account, config: BridgeConfig, pop3_port: int, smtp_port: int) -> Tuple[str, ...]:
    """What to type into the device's Inbox service settings."""
    custom_ports = pop3_port != DEFAULT_POP3_PORT or smtp_port != DEFAULT_SMTP_PORT
    address = account.setting("email") or account.setting("username") or ""
    lines = (
        f"mail bridge for account {account.name!r} ({account.backend}) listening on {config.listen}: "
        f"POP3 port {pop3_port}, SMTP port {smtp_port}",
        "on the Jornada, Inbox > Services > Options > Add: POP3 Mail, then enter:",
        f"  POP3 host: {config.listen}    User ID: {config.local_user}    Password: the local_password of this account",
        f"  SMTP host for outgoing mail: {config.listen}",
        f"  return address: {address}" if "@" in address else "",
        (f"  the bridge uses ports {pop3_port}/{smtp_port} instead of 110/25; set the same ports in the service"
         if custom_ports else ""),
        "press Ctrl-C to stop the bridge",
    )
    return tuple(line for line in lines if line)


def _backend_for(account: Account, secrets: Dict[str, Any], context: BuildContext) -> MailBackend:
    override = context.extra.get(BACKEND_HOOK)
    if override is not None:
        return override
    from .backends import backend_spec  # local import: the backends package imports this module's peers

    return backend_spec(account.backend).build(account, secrets, context)


def run_bridge(account: Account, secrets: Dict[str, Any], context: BuildContext) -> int:
    """Start the bridge for ``account`` and serve until Ctrl-C; 1 when it cannot start or connect."""
    try:
        config = config_from_account(account, secrets, context)
        backend = _backend_for(account, secrets, context)
    except (MailError, AccountError) as exc:
        context.log(f"error: {exc}")
        return 1
    bridge = MailBridge(backend, config, context.log)
    try:
        bridge.backend.connect()
    except MailError as exc:
        context.log(f"error: cannot connect to {bridge.backend.name}: {exc}")
        return 1
    try:
        bridge.start()
    except OSError as exc:
        bridge.backend.close()
        context.log(bind_error(config, exc))
        return 1
    for line in instructions(account, config, bridge.pop3_port, bridge.smtp_port):
        context.log(line)
    on_start = context.extra.get(START_HOOK)
    if callable(on_start):
        on_start(bridge)
    try:
        bridge.serve_forever()
    except KeyboardInterrupt:
        context.log("stopping the mail bridge")
    finally:
        bridge.stop()
    return 0
