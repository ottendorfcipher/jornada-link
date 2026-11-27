"""Apple Reminders backend: one list in Reminders.app ⇄ the device's tasks, through JXA.

Each operation is one JavaScript-for-Automation script run by ``osascript``. The
scripts are constants; the list name and the task's fields (one JSON argument)
travel as ``argv`` entries and are never interpolated into script source. Dates
cross the boundary as ISO 8601 text: a due date is set to local midnight in the
script and read back as the local calendar day. Reminders has no categories,
start dates or privacy flag, so those fields do not travel.
"""
from __future__ import annotations

import json
from datetime import tzinfo
from typing import Any, Dict, Optional, Tuple

from ...pim.models import Task
from ...webapi.applescript import AppleScriptError, Runner, run_jxa
from ..accounts import Account
from ..base import Item, StoreError
from ..registry import BackendSpec, BuildContext, SettingSpec
from .common import NO_SUBJECT, Log, completion_date, date_of, is_real_completion
from .filters import wrap_completed

SERVICE = "Apple Reminders"
DEFAULT_LIST = "Jornada"
LIST_SETTING = "list"
PRIORITY_CODES = {"high": 1, "normal": 0, "low": 9}

_PRELUDE = """
function remindersApp() { return Application("Reminders"); }
function listNamed(Reminders, name) {
  const list = Reminders.lists.byName(name);
  if (list.exists()) { return list; }
  Reminders.lists.push(Reminders.List({name: name}));
  return Reminders.lists.byName(name);
}
function reminderIn(list, id) {
  const reminder = list.reminders.byId(id);
  if (!reminder.exists()) { throw new Error("the reminder is no longer in the list"); }
  return reminder;
}
function stamp(value) { return value ? new Date(value).toISOString() : ""; }
function localMidnight(text) {
  if (!text) { return null; }
  const parts = text.split("-").map(Number);
  return new Date(parts[0], parts[1] - 1, parts[2]);
}
function tryGet(getter) { try { return getter(); } catch (error) { return null; } }
function setFields(reminder, fields) {
  reminder.name = fields.name;
  reminder.body = fields.body;
  reminder.priority = fields.priority;
  try { reminder.dueDate = localMidnight(fields.due); } catch (error) { if (fields.due) { throw error; } }
  reminder.completed = fields.completed === true;
  if (fields.completed && fields.completionDate) {
    try { reminder.completionDate = localMidnight(fields.completionDate); } catch (error) {}
  }
}
"""

LIST_SCRIPT = _PRELUDE + """
function run(argv) {
  const Reminders = remindersApp();
  const reminders = listNamed(Reminders, argv[0]).reminders;
  const ids = reminders.id(), names = reminders.name(), bodies = reminders.body();
  const dues = reminders.dueDate(), done = reminders.completed(), completions = reminders.completionDate();
  const priorities = reminders.priority(), modified = reminders.modificationDate();
  const alldays = tryGet(function () { return reminders.alldayDueDate(); }) || [];
  return JSON.stringify(ids.map(function (id, i) {
    return {id: id, name: names[i], body: bodies[i], dueDate: stamp(dues[i] || alldays[i]),
            completed: done[i] === true, completionDate: stamp(completions[i]),
            priority: priorities[i], modified: stamp(modified[i])};
  }));
}
"""

CREATE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Reminders = remindersApp();
  const list = listNamed(Reminders, argv[0]);
  const fields = JSON.parse(argv[1]);
  const properties = {name: fields.name, body: fields.body, priority: fields.priority};
  if (fields.due) { properties.dueDate = localMidnight(fields.due); }
  const reminder = Reminders.Reminder(properties);
  list.reminders.push(reminder);
  if (fields.completed) { setFields(reminder, fields); }
  return JSON.stringify({id: reminder.id(), modified: stamp(reminder.modificationDate())});
}
"""

UPDATE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Reminders = remindersApp();
  const reminder = reminderIn(listNamed(Reminders, argv[0]), argv[1]);
  setFields(reminder, JSON.parse(argv[2]));
  return JSON.stringify({id: reminder.id(), modified: stamp(reminder.modificationDate())});
}
"""

DELETE_SCRIPT = _PRELUDE + """
function run(argv) {
  const Reminders = remindersApp();
  const reminder = listNamed(Reminders, argv[0]).reminders.byId(argv[1]);
  const existed = reminder.exists();
  if (existed) { reminder.delete(); }
  return JSON.stringify({id: argv[1], deleted: existed});
}
"""


def priority_name(code: Any) -> str:
    """Reminders' 0-9 scale → high (1-4), normal (0, 5) or low (6-9)."""
    value = code if isinstance(code, int) and not isinstance(code, bool) else 0
    if 1 <= value <= 4:
        return "high"
    if 6 <= value <= 9:
        return "low"
    return "normal"


def fields_of(record: Task) -> Dict[str, Any]:
    """The JSON document the create/update scripts apply to a reminder."""
    item = record.normalized()
    return {
        "name": item.summary or NO_SUBJECT,
        "body": item.notes,
        "due": item.due.isoformat() if item.due else "",
        "priority": PRIORITY_CODES.get(item.priority, 0),
        "completed": item.is_completed,
        "completionDate": item.completed.isoformat() if is_real_completion(item.completed) else "",
    }


def _id_of(answer: Any, what: str) -> str:
    if not isinstance(answer, dict) or not isinstance(answer.get("id"), str) or not answer["id"]:
        raise StoreError(f"{SERVICE} did not report the id of the reminder it should {what}")
    return answer["id"]


def _version_of(answer: Any) -> Optional[str]:
    modified = answer.get("modified") if isinstance(answer, dict) else None
    return modified if isinstance(modified, str) and modified else None


class AppleRemindersStore:
    """Store protocol over the reminders of one list (created when missing)."""

    name = "apple-reminders"

    def __init__(self, list_name: str = DEFAULT_LIST, runner: Optional[Runner] = None,
                 log: Log = lambda _line: None, zone: Optional[tzinfo] = None) -> None:
        self._list = list_name.strip() or DEFAULT_LIST
        self._runner = runner
        self._log = log
        self._zone = zone

    @property
    def list_name(self) -> str:
        return self._list

    def _run(self, script: str, args: Tuple[str, ...], what: str) -> Any:
        try:
            return run_jxa(script, args, runner=self._runner)
        except AppleScriptError as exc:
            raise StoreError(f"{SERVICE} could not {what}: {exc}") from exc

    # -- Store protocol -------------------------------------------------------
    def list(self) -> Tuple[Item, ...]:
        answer = self._run(LIST_SCRIPT, (self._list,), f"list the reminders of {self._list!r}")
        if not isinstance(answer, list):
            raise StoreError(f"{SERVICE} returned an unexpected listing")
        return tuple(self._item(entry) for entry in answer)

    def _item(self, entry: Any) -> Item:
        reminder_id = _id_of(entry, "list")
        completed = entry.get("completed") is True
        record = Task(
            summary=str(entry.get("name") or ""),
            due=date_of(entry.get("dueDate"), self._zone),
            completed=completion_date(completed, entry.get("completionDate"), self._zone),
            priority=priority_name(entry.get("priority")),
            notes=str(entry.get("body") or ""),
            uid=reminder_id,
        )
        return Item(id=reminder_id, record=record, version=_version_of(entry))

    def create(self, record: Task) -> str:
        payload = json.dumps(fields_of(record), ensure_ascii=False)
        answer = self._run(CREATE_SCRIPT, (self._list, payload), f"create the reminder {record.summary!r}")
        return _id_of(answer, "create")

    def update(self, item_id: str, record: Task) -> Optional[str]:
        _check_id(item_id)
        payload = json.dumps(fields_of(record), ensure_ascii=False)
        answer = self._run(UPDATE_SCRIPT, (self._list, item_id, payload), f"update the reminder {record.summary!r}")
        return _version_of(answer)

    def delete(self, item_id: str) -> None:
        _check_id(item_id)
        answer = self._run(DELETE_SCRIPT, (self._list, item_id), "delete the reminder")
        if isinstance(answer, dict) and answer.get("deleted") is False:
            self._log(f"reminder {item_id} was already gone from {SERVICE}")


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id.strip():
        raise StoreError(f"{SERVICE} needs a reminder id to update or delete")


def build(account: Account, secrets: Dict[str, Any], context: BuildContext) -> Any:
    store = AppleRemindersStore(list_name=account.setting(LIST_SETTING, DEFAULT_LIST) or DEFAULT_LIST,
                                runner=context.runner, log=context.log)
    return wrap_completed(store, account, context.log)


BACKEND = BackendSpec(
    key="apple",
    title="Apple Reminders (Reminders.app)",
    settings=(
        SettingSpec(LIST_SETTING, f"reminder list to sync (default {DEFAULT_LIST}; created if missing)",
                    required=False, default=DEFAULT_LIST),
    ),
    build=build,
    notes="Drives Reminders through osascript; macOS asks for Automation permission on the first run. "
          "Reminders has no categories, start dates or private flag, so those fields do not travel.",
)

__all__ = ["AppleRemindersStore", "BACKEND", "build", "fields_of", "priority_name",
           "LIST_SCRIPT", "CREATE_SCRIPT", "UPDATE_SCRIPT", "DELETE_SCRIPT", "DEFAULT_LIST"]
