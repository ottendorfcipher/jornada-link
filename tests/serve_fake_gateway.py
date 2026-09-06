"""Serve the fake jornada-gpib gateway and print its port (for the Swift SelfTest).

Usage:  python3 tests/serve_fake_gateway.py [seconds]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_gateway import FakeGateway  # noqa: E402


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    gateway = FakeGateway().start()
    print(gateway.port, flush=True)
    time.sleep(seconds)
    gateway.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
