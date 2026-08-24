"""Start a populated in-memory fake Jornada and print its RAPI port.

Used by CI and by contributors to exercise the Swift client (SelfTest) or the
CLI without real hardware. Prints the chosen TCP port on the first line, then
serves until the timeout elapses or the process is killed.

Usage:  python3 tests/serve_fake.py [seconds]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_device import FakeFilesystem, FakeRapiServer  # noqa: E402


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    filesystem = FakeFilesystem()
    filesystem.dirs.update({"\\My Documents", "\\Windows"})
    filesystem.files["\\My Documents\\notes.txt"] = b"hello jornada\n"
    filesystem.files["\\Windows\\pword.exe"] = b"MZ fake"
    server = FakeRapiServer(filesystem).start()
    print(server.port, flush=True)
    time.sleep(seconds)
    server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
