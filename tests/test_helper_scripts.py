"""Static safety checks for the privileged connect helpers and installer.

These scripts run as root under a NOPASSWD sudoers rule, so they are a security
boundary. We can't exercise the privileged install without a password, but we
can lock down the properties that make them safe.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
CONNECT = BIN / "jornada-connect"
DISCONNECT = BIN / "jornada-disconnect"
INSTALLER = BIN / "jornada-setup-passwordless"
HELPERS = [CONNECT, DISCONNECT]

# A shell positional parameter ($1, ${1, $@, $*) — awk's $2 etc. use single
# quotes around the program, so we look for the shell forms specifically.
SHELL_POSITIONAL = re.compile(r'\$\{?[1-9]|\$@|\$\*')


def _awk_stripped(text: str) -> str:
    """Remove awk program bodies so their $2 fields don't look like shell args."""
    return re.sub(r"awk '[^']*'", "awk ''", text)


@pytest.mark.parametrize("script", HELPERS + [INSTALLER])
def test_scripts_are_executable_and_valid_sh(script: Path):
    assert script.exists(), f"missing {script}"
    assert script.stat().st_mode & 0o111, f"{script} is not executable"
    result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, f"{script} failed sh -n: {result.stderr}"


@pytest.mark.parametrize("script", HELPERS)
def test_privileged_helpers_take_no_arguments(script: Path):
    # The NOPASSWD sudoers rule matches these argument-free; if they consumed
    # positionals, a caller could smuggle behavior past the rule.
    body = _awk_stripped(script.read_text())
    assert not SHELL_POSITIONAL.search(body), f"{script} references a shell positional parameter"


@pytest.mark.parametrize("script", HELPERS)
def test_privileged_helpers_require_root(script: Path):
    assert 'id -u' in script.read_text(), f"{script} lacks a root check"


@pytest.mark.parametrize("script", HELPERS)
def test_privileged_helpers_do_not_source_files(script: Path):
    # Sourcing anything (especially from a user-writable path) would let a local
    # attacker run code as root. These helpers must never source.
    for line in script.read_text().splitlines():
        stripped = line.strip()
        assert not stripped.startswith(". "), f"{script} sources a file: {line!r}"
        assert not stripped.startswith("source "), f"{script} sources a file: {line!r}"


@pytest.mark.parametrize("script", HELPERS)
def test_helpers_clear_legacy_engines(script: Path):
    # Regression: a legacy admin-prompt runner (run-ppp.sh) left alive fights the
    # helper's loop for the serial port ("Resource busy" every 2s). Both helpers
    # must kill retry loops and wrapper shells, not just pppd.
    body = script.read_text()
    assert "run-ppp[.]sh" in body, f"{script} must kill the legacy run-ppp.sh loop"
    assert "bin/jornada[-]ppp" in body, f"{script} must kill CLI wrapper shells"
    assert "pkill -9 -f 'pppd /dev/cu[.]usbserial'" in body, f"{script} must SIGKILL wedged pppd"


def test_installer_runs_cleanup_after_install():
    # The reinstall is the one authenticated step, so it must also clear any
    # legacy engine that would otherwise sabotage the new helper.
    assert '"$DISCONNECT"' in INSTALLER.read_text()


def test_connect_validates_serial_device():
    body = CONNECT.read_text()
    assert "/dev/cu." in body, "connect helper must constrain the device path"
    assert "invalid characters" in body, "connect helper must reject metacharacters"


def test_connect_loop_carries_process_marker():
    # The detached retry loop must be findable by health checks even while pppd
    # is between restarts; the argv-0 operand after the sh -c script is that
    # marker. An anonymous loop was invisible and caused false "engine dead"
    # diagnoses (and real cleanup gaps).
    body = CONNECT.read_text()
    assert "' jornada-ppp-loop " in body, "loop shell must carry the jornada-ppp-loop argv marker"


def test_disconnect_kills_marked_loop():
    body = DISCONNECT.read_text()
    assert "jornada-ppp-loop" in body, "disconnect must also target the marked loop"


def test_connect_only_reads_root_owned_config():
    # Config must come from the root-owned /usr/local/etc path, never $HOME.
    body = CONNECT.read_text()
    assert "/usr/local/etc/jornada-link/config" in body
    assert "$HOME/.jornada-link/config" not in body


def test_installer_refuses_non_root():
    result = subprocess.run([str(INSTALLER)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "must run as root" in result.stdout.lower() + result.stderr.lower()


def test_generated_sudoers_is_valid():
    visudo = shutil.which("visudo") or ("/usr/sbin/visudo" if Path("/usr/sbin/visudo").exists() else None)
    if not visudo:
        pytest.skip("visudo not available")
    connect = "/usr/local/libexec/jornada-link/jornada-connect"
    disconnect = "/usr/local/libexec/jornada-link/jornada-disconnect"
    content = (
        "# test\n"
        f"testuser ALL=(root) NOPASSWD: {connect}\n"
        f"testuser ALL=(root) NOPASSWD: {disconnect}\n"
    )
    tmp = Path(subprocess.run(["mktemp"], capture_output=True, text=True).stdout.strip())
    try:
        tmp.write_text(content)
        result = subprocess.run([visudo, "-cf", str(tmp)], capture_output=True, text=True)
        assert result.returncode == 0, f"visudo rejected the generated rule: {result.stdout}{result.stderr}"
    finally:
        tmp.unlink(missing_ok=True)
