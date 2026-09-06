"""Validate a mirror tree written by the SWIFT implementation against the
Python implementation's expectations. Run by the CI Swift job and locally:

    macapp/.build/debug/SelfTest mirror /tmp/somedir
    python3 tests/check_mirror_parity.py /tmp/somedir
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main(root_text: str) -> int:
    root = Path(root_text)
    target = root / "My Documents" / "parity.txt"
    assert target.read_bytes() == b"two", "canonical file must hold the latest content"
    archived = [p for p in target.parent.iterdir()
                if p.name.startswith("parity.") and p.name.endswith(".txt") and p != target]
    assert len(archived) == 1, f"expected one archived version, got {archived}"
    assert archived[0].read_bytes() == b"one", "archived copy must hold the prior content"

    evil = list(root.glob("**/*evil*"))
    assert evil, "hostile path must be mirrored (sanitized)"
    assert all(root.resolve() in p.resolve().parents for p in evil), "sanitized path escaped root"

    lines = [json.loads(line) for line in (root / "sent-manifest.jsonl").read_text().splitlines()]
    assert len(lines) == 4, f"expected 4 manifest entries, got {len(lines)}"
    for key in ("ts", "device_path", "size", "md5", "mirror", "unchanged", "source"):
        assert key in lines[0], f"manifest missing key {key}"
    assert [line["unchanged"] for line in lines[:3]] == [False, True, False]
    assert lines[0]["md5"] == hashlib.md5(b"one").hexdigest()
    assert lines[2]["md5"] == hashlib.md5(b"two").hexdigest()
    print("mirror parity OK: Swift tree validates against Python expectations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
