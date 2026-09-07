import os

import pytest

from jornada.webapi.applescript import (AppleScriptError, applescript_string, fake_runner, is_available, js_string,
                                        run_applescript, run_jxa, subprocess_runner)


def test_run_jxa_parses_json_and_passes_args():
    calls = []
    runner = fake_runner([{"a": 1}, "", "not json"], calls)
    assert run_jxa("function run(argv) { return JSON.stringify({a: 1}) }", ["x", "y"], runner=runner) == {"a": 1}
    argv, stdin, _timeout = calls[0]
    assert argv[:4] == ("/usr/bin/osascript", "-l", "JavaScript", "-") and argv[4:] == ("x", "y") and "run(argv)" in stdin
    assert run_jxa("x", runner=runner) is None
    with pytest.raises(AppleScriptError):
        run_jxa("x", runner=runner)


def test_run_applescript_and_errors():
    runner = fake_runner(["hello\n", RuntimeError("Calendar got an error: not allowed")])
    assert run_applescript('return "hello"', runner=runner) == "hello"
    with pytest.raises(AppleScriptError) as info:
        run_applescript("x", runner=runner)
    assert "not allowed" in str(info.value)
    with pytest.raises(AppleScriptError):
        run_applescript("x", runner=fake_runner([]))


def test_literal_helpers():
    assert js_string('a"b\n') == '"a\\"b\\n"' and js_string({"k": "v"}) == '{"k": "v"}'
    assert applescript_string('say "hi"\\') == '"say \\"hi\\"\\\\"'


def test_subprocess_runner_missing_binary_and_real_osascript():
    with pytest.raises(AppleScriptError):
        subprocess_runner(["/nonexistent/osascript"], b"", 5)
    if not is_available() or os.environ.get("CI"):
        pytest.skip("osascript not available")
    assert run_applescript('return "ok"', runner=subprocess_runner) == "ok"
    assert run_jxa("function run() { return JSON.stringify([1, 2]) }", runner=subprocess_runner) == [1, 2]
