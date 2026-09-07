"""Run AppleScript / JavaScript-for-Automation through ``osascript`` (a system binary).

Apple's own apps (Calendar, Contacts, Reminders, Notes, Mail, Pages, Numbers)
are reached this way from the CLI. Scripts are fed on stdin and arguments are
passed as separate argv entries, so values never need shell quoting. The
runner is injectable, which is how tests supply canned answers.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable, Optional, Sequence, Tuple

Runner = Callable[[Sequence[str], Optional[bytes], float], Tuple[int, bytes, bytes]]
OSASCRIPT = "/usr/bin/osascript"


class AppleScriptError(RuntimeError):
    """osascript failed; the message is its stderr, trimmed."""


def subprocess_runner(argv: Sequence[str], stdin: Optional[bytes], timeout: float) -> Tuple[int, bytes, bytes]:
    try:
        completed = subprocess.run(list(argv), input=stdin, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AppleScriptError(f"{argv[0]} is not available on this Mac") from exc
    except subprocess.TimeoutExpired as exc:
        raise AppleScriptError(f"osascript did not answer within {timeout:.0f}s") from exc
    return completed.returncode, completed.stdout, completed.stderr


def is_available() -> bool:
    return shutil.which("osascript") is not None


def _run(language: str, script: str, args: Sequence[str], runner: Optional[Runner], timeout: float) -> str:
    run = runner or subprocess_runner
    argv = [OSASCRIPT, "-l", language, "-", *args]
    code, out, err = run(argv, script.encode("utf-8"), timeout)
    if code != 0:
        message = err.decode("utf-8", errors="replace").strip() or f"exit status {code}"
        raise AppleScriptError(message[:500])
    return out.decode("utf-8", errors="replace")


def run_applescript(script: str, args: Sequence[str] = (), runner: Optional[Runner] = None,
                    timeout: float = 120.0) -> str:
    """Run AppleScript; returns stdout without the trailing newline."""
    return _run("AppleScript", script, args, runner, timeout).rstrip("\n")


def run_jxa(script: str, args: Sequence[str] = (), runner: Optional[Runner] = None,
            timeout: float = 120.0) -> Any:
    """Run JavaScript for Automation whose ``run(argv)`` returns a JSON string; returns the parsed value."""
    text = _run("JavaScript", script, args, runner, timeout).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise AppleScriptError(f"script did not return JSON: {text[:200]}") from exc


def js_string(value: Any) -> str:
    """A JavaScript literal for embedding a Python value in JXA source."""
    return json.dumps(value, ensure_ascii=False)


def applescript_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def fake_runner(answers: Sequence[Any], calls: Optional[list] = None) -> Runner:
    """For tests: each call returns the next canned answer (JSON-encoded when not bytes/str)."""
    queue = list(answers)

    def run(argv: Sequence[str], stdin: Optional[bytes], timeout: float) -> Tuple[int, bytes, bytes]:
        if calls is not None:
            calls.append((tuple(argv), stdin.decode("utf-8") if stdin else "", timeout))
        if not queue:
            return 1, b"", b"no canned answer left"
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            return 1, b"", str(answer).encode("utf-8")
        if isinstance(answer, bytes):
            return 0, answer, b""
        if isinstance(answer, str):
            return 0, answer.encode("utf-8"), b""
        return 0, json.dumps(answer).encode("utf-8"), b""

    return run
