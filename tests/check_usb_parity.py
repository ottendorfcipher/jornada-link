"""Compare the SWIFT USB link table with the Python one. Run by CI and locally:

    macapp/.build/debug/SelfTest usb-profiles /tmp/usb-table.json
    python3 tests/check_usb_parity.py /tmp/usb-table.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jornada import usb_profiles  # noqa: E402


def differences(swift: dict, python: dict) -> list:
    """Human-readable mismatches between the two tables (empty when identical)."""
    found = []
    for section in ("drivers", "handhelds"):
        swift_rows = {row.get("key"): row for row in swift.get(section, [])}
        python_rows = {row["key"]: row for row in python[section]}
        for key in sorted(set(swift_rows) | set(python_rows)):
            if key not in swift_rows:
                found.append(f"{section}: {key} missing from Swift")
            elif key not in python_rows:
                found.append(f"{section}: {key} missing from Python")
            elif swift_rows[key] != python_rows[key]:
                fields = sorted(f for f in set(swift_rows[key]) | set(python_rows[key])
                                if swift_rows[key].get(f) != python_rows[key].get(f))
                found.append(f"{section}: {key} differs in {', '.join(fields)}")
    if list(swift.get("dock_markers", [])) != python["dock_markers"]:
        found.append("dock_markers differ")
    return found


def main(path_text: str) -> int:
    swift = json.loads(Path(path_text).read_text(encoding="utf-8"))
    problems = differences(swift, usb_profiles.table())
    for line in problems:
        print(f"MISMATCH  {line}")
    if problems:
        return 1
    print(f"usb table parity OK: {len(swift['drivers'])} driver profiles, "
          f"{len(swift['handhelds'])} handheld rows agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
