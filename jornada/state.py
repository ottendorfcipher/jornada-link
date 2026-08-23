"""Connection-state file shared between the dccm listener and the CLI tools."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_STATE_PATH = Path.home() / ".jornada-link" / "connection.json"


def write_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomically write ``state`` as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".connection-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def read_state(path: Path) -> Optional[Dict[str, Any]]:
    """Return the saved state, or None if absent/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_state(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
