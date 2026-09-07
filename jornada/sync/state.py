"""Per-account sync state: which device record is linked to which remote item, and the
content hashes both sides had when they were last in agreement."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..state import read_state, write_state
from .base import StoreError

STATE_VERSION = 1


@dataclass(frozen=True)
class Link:
    local_id: str
    remote_id: str
    local_hash: str
    remote_hash: str


@dataclass(frozen=True)
class SyncState:
    links: Tuple[Link, ...] = ()
    last_sync: Optional[str] = None
    remote_token: Optional[str] = None

    def by_local(self) -> Dict[str, Link]:
        return {link.local_id: link for link in self.links}

    def by_remote(self) -> Dict[str, Link]:
        return {link.remote_id: link for link in self.links}

    def with_link(self, link: Link) -> "SyncState":
        kept = tuple(l for l in self.links if l.local_id != link.local_id and l.remote_id != link.remote_id)
        return replace(self, links=kept + (link,))

    def without(self, local_id: Optional[str] = None, remote_id: Optional[str] = None) -> "SyncState":
        kept = tuple(l for l in self.links
                     if not ((local_id is not None and l.local_id == local_id)
                             or (remote_id is not None and l.remote_id == remote_id)))
        return replace(self, links=kept)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "last_sync": self.last_sync,
            "remote_token": self.remote_token,
            "links": [{"local": l.local_id, "remote": l.remote_id,
                       "local_hash": l.local_hash, "remote_hash": l.remote_hash} for l in self.links],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SyncState":
        links = tuple(Link(str(l["local"]), str(l["remote"]), str(l.get("local_hash", "")),
                           str(l.get("remote_hash", ""))) for l in data.get("links", []))
        return cls(links=links, last_sync=data.get("last_sync"), remote_token=data.get("remote_token"))


class StateError(StoreError):
    """The state file exists but cannot be used; a silent fresh start would re-pair everything."""


def load_state(path: Path) -> SyncState:
    if not path.exists():
        return SyncState()
    data = read_state(path)
    if data is None:
        raise StateError(f"the sync state file {path} is unreadable; move it aside to start over "
                         "(records are then re-paired by content)")
    try:
        return SyncState.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError(f"the sync state file {path} is malformed ({exc}); move it aside to start over") from exc


def save_state(path: Path, state: SyncState) -> None:
    write_state(path, state.to_dict())
