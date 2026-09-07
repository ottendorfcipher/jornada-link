"""Three-way sync between a device store and a modern store.

``plan`` is a pure function of both listings and the saved state; ``apply``
executes a plan and returns the new state. Change detection compares each
record's content fingerprint with the one stored in its link, so no side needs
modification stamps. Records never seen before are paired by their
``match_key`` (subject + start, name + email, ...) so a first sync of two
populated sides does not duplicate everything.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..rapi_errors import RapiError
from .base import Item, Store, StoreError
from .state import Link, SyncState


class Direction(str, Enum):
    BOTH = "both"
    TO_DEVICE = "to-device"
    FROM_DEVICE = "from-device"


class Prefer(str, Enum):
    REMOTE = "remote"
    LOCAL = "local"


@dataclass(frozen=True)
class Options:
    direction: Direction = Direction.BOTH
    prefer: Prefer = Prefer.REMOTE
    propagate_deletes: bool = True

    @property
    def writes_local(self) -> bool:
        return self.direction in (Direction.BOTH, Direction.TO_DEVICE)

    @property
    def writes_remote(self) -> bool:
        return self.direction in (Direction.BOTH, Direction.FROM_DEVICE)


@dataclass(frozen=True)
class Action:
    kind: str
    local_id: Optional[str] = None
    remote_id: Optional[str] = None
    record: Any = None
    reason: str = ""

    def describe(self) -> str:
        label = _label(self.record)
        where = {"create_local": "→ device", "update_local": "→ device", "delete_local": "✕ device",
                 "create_remote": "→ remote", "update_remote": "→ remote", "delete_remote": "✕ remote",
                 "link": "= link", "unlink": "≠ unlink", "conflict": "! conflict", "skip": "· skip"}
        return f"{where.get(self.kind, self.kind):10} {label}{'  (' + self.reason + ')' if self.reason else ''}"


@dataclass(frozen=True)
class Plan:
    actions: Tuple[Action, ...] = ()

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)

    @property
    def is_empty(self) -> bool:
        return all(a.kind in ("link", "skip") for a in self.actions)

    @property
    def writes_device(self) -> bool:
        return any(a.kind in ("create_local", "update_local", "delete_local") for a in self.actions)

    def summary(self) -> str:
        parts = [f"{self.count(k)} {k.replace('_', ' ')}" for k in
                 ("create_local", "update_local", "delete_local", "create_remote", "update_remote",
                  "delete_remote", "link", "conflict") if self.count(k)]
        return ", ".join(parts) if parts else "nothing to do"


@dataclass
class Result:
    counts: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    touched_local: Set[str] = field(default_factory=set)
    touched_remote: Set[str] = field(default_factory=set)

    def bump(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def summary(self) -> str:
        parts = [f"{n} {k.replace('_', ' ')}" for k, n in self.counts.items()]
        text = ", ".join(parts) if parts else "nothing changed"
        return f"{text}; {len(self.errors)} error(s)" if self.errors else text


def _label(record: Any) -> str:
    for attr in ("summary", "title", "name"):
        value = getattr(record, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()[:60]
    if hasattr(record, "display_name"):
        return record.display_name()[:60]
    return "(record)"


# -- planning -------------------------------------------------------------------
def plan(local: Sequence[Item], remote: Sequence[Item], state: SyncState,
         options: Options = Options()) -> Plan:
    local_by_id = {item.id: item for item in local}
    remote_by_id = {item.id: item for item in remote}
    actions: List[Action] = []
    linked_local: Set[str] = set()
    linked_remote: Set[str] = set()
    for link in state.links:
        linked_local.add(link.local_id)
        linked_remote.add(link.remote_id)
        actions.extend(_plan_link(link, local_by_id.get(link.local_id), remote_by_id.get(link.remote_id), options))
    new_local = [item for item in local if item.id not in linked_local]
    new_remote = [item for item in remote if item.id not in linked_remote]
    actions.extend(_pair_new(new_local, new_remote, options))
    return Plan(tuple(actions))


def _plan_link(link: Link, local: Optional[Item], remote: Optional[Item], options: Options) -> List[Action]:
    if local is None and remote is None:
        return [Action("unlink", link.local_id, link.remote_id, reason="gone on both sides")]
    if remote is None:
        if options.propagate_deletes and options.writes_local:
            return [Action("delete_local", link.local_id, link.remote_id, local.record, "deleted remotely")]
        return [Action("unlink", link.local_id, link.remote_id, local.record, "deleted remotely; device copy kept")]
    if local is None:
        if options.propagate_deletes and options.writes_remote:
            return [Action("delete_remote", link.local_id, link.remote_id, remote.record, "deleted on the device")]
        return [Action("unlink", link.local_id, link.remote_id, remote.record, "deleted on the device; remote copy kept")]
    local_changed = local.fingerprint != link.local_hash
    remote_changed = remote.fingerprint != link.remote_hash
    if not local_changed and not remote_changed:
        return []
    if local_changed and remote_changed and local.fingerprint == remote.fingerprint:
        return [Action("link", local.id, remote.id, local.record, "same change on both sides")]
    if local_changed and remote_changed:
        return [_resolve_conflict(local, remote, options)]
    if local_changed:
        return [_push(local, remote, options, "changed on the device")]
    return [_pull(local, remote, options, "changed remotely")]


def _push(local: Item, remote: Item, options: Options, reason: str) -> Action:
    if options.writes_remote:
        return Action("update_remote", local.id, remote.id, local.record, reason)
    return Action("skip", local.id, remote.id, local.record, reason + "; not writing remote")


def _pull(local: Item, remote: Item, options: Options, reason: str) -> Action:
    if options.writes_local:
        return Action("update_local", local.id, remote.id, remote.record, reason)
    return Action("skip", local.id, remote.id, remote.record, reason + "; not writing device")


def _resolve_conflict(local: Item, remote: Item, options: Options) -> Action:
    """Both sides changed: in one-way mode the source side wins, otherwise ``prefer`` decides."""
    pull = Action("update_local", local.id, remote.id, remote.record, "changed on both sides; remote wins")
    push = Action("update_remote", local.id, remote.id, local.record, "changed on both sides; device wins")
    if not options.writes_local:
        return push if options.writes_remote else Action("conflict", local.id, remote.id, local.record, "changed on both sides; left as is")
    if not options.writes_remote:
        return pull
    return pull if options.prefer == Prefer.REMOTE else push


def _pair_new(new_local: Sequence[Item], new_remote: Sequence[Item], options: Options) -> List[Action]:
    actions: List[Action] = []
    unmatched: Dict[str, List[Item]] = {}
    for item in new_remote:
        unmatched.setdefault(item.match_key, []).append(item)
    for item in new_local:
        candidates = unmatched.get(item.match_key)
        if candidates:
            partner = candidates.pop(0)
            actions.extend(_pair(item, partner, options))
            continue
        if options.writes_remote:
            actions.append(Action("create_remote", item.id, None, item.record, "new on the device"))
        else:
            actions.append(Action("skip", item.id, None, item.record, "new on the device; not writing remote"))
    for remaining in unmatched.values():
        for item in remaining:
            if options.writes_local:
                actions.append(Action("create_local", None, item.id, item.record, "new remotely"))
            else:
                actions.append(Action("skip", None, item.id, item.record, "new remotely; not writing device"))
    return actions


def _pair(local: Item, remote: Item, options: Options) -> List[Action]:
    link = Action("link", local.id, remote.id, local.record, "matched by content")
    if local.fingerprint == remote.fingerprint:
        return [link]
    return [link, _resolve_conflict(local, remote, replace(options))]


# -- applying -------------------------------------------------------------------
def apply(plan_: Plan, local: Store, remote: Store, state: SyncState,
          log: Callable[[str], None] = lambda _line: None,
          now: Callable[[], float] = time.time) -> Tuple[SyncState, Result]:
    result = Result()
    current = state
    for action in plan_.actions:
        try:
            current = _apply_one(action, local, remote, current, result)
            if action.kind not in ("skip",):
                log(action.describe())
        except (StoreError, RapiError, OSError, ValueError) as exc:
            result.errors.append(f"{action.describe()}: {exc}")
            log(f"!! {action.describe()}: {exc}")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now()))
    return replace(current, last_sync=stamp), result


def _apply_one(action: Action, local: Store, remote: Store, state: SyncState, result: Result) -> SyncState:
    kind, record = action.kind, action.record
    fingerprint = record.fingerprint() if record is not None else ""
    if kind == "create_remote":
        remote_id = remote.create(record)
        result.bump(kind)
        result.touched_remote.add(remote_id)
        return state.with_link(Link(action.local_id, remote_id, fingerprint, fingerprint))
    if kind == "create_local":
        local_id = local.create(record)
        result.bump(kind)
        result.touched_local.add(local_id)
        return state.with_link(Link(local_id, action.remote_id, fingerprint, fingerprint))
    if kind == "update_remote":
        remote.update(action.remote_id, record)
        result.bump(kind)
        result.touched_remote.add(action.remote_id)
        return state.with_link(Link(action.local_id, action.remote_id, fingerprint, fingerprint))
    if kind == "update_local":
        local.update(action.local_id, record)
        result.bump(kind)
        result.touched_local.add(action.local_id)
        return state.with_link(Link(action.local_id, action.remote_id, fingerprint, fingerprint))
    if kind == "delete_local":
        local.delete(action.local_id)
        result.bump(kind)
        return state.without(local_id=action.local_id)
    if kind == "delete_remote":
        remote.delete(action.remote_id)
        result.bump(kind)
        return state.without(remote_id=action.remote_id)
    if kind == "link":
        result.bump(kind)
        return state.with_link(Link(action.local_id, action.remote_id, fingerprint, fingerprint))
    if kind == "unlink":
        result.bump(kind)
        return state.without(local_id=action.local_id, remote_id=action.remote_id)
    result.bump(kind)
    return state


def refresh_hashes(state: SyncState, local: Sequence[Item], remote: Sequence[Item],
                   touched_local: Set[str], touched_remote: Set[str]) -> SyncState:
    """After writing, record what each side actually stored so normalization differences
    (a device that trims text, a server that reformats dates) do not read as new edits."""
    local_by_id = {item.id: item for item in local}
    remote_by_id = {item.id: item for item in remote}
    links = []
    for link in state.links:
        local_hash, remote_hash = link.local_hash, link.remote_hash
        if link.local_id in touched_local and link.local_id in local_by_id:
            local_hash = local_by_id[link.local_id].fingerprint
        if link.remote_id in touched_remote and link.remote_id in remote_by_id:
            remote_hash = remote_by_id[link.remote_id].fingerprint
        links.append(Link(link.local_id, link.remote_id, local_hash, remote_hash))
    return replace(state, links=tuple(links))
