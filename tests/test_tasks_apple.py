import json
from datetime import date, timedelta, timezone

import pytest

from jornada.pim.models import Task
from jornada.pim.tasks import UNKNOWN_COMPLETION_DATE
from jornada.sync.accounts import Account
from jornada.sync.base import StoreError
from jornada.sync.registry import BuildContext
from jornada.sync.tasks import apple
from jornada.sync.tasks.apple import (CREATE_SCRIPT, DELETE_SCRIPT, LIST_SCRIPT, UPDATE_SCRIPT, AppleRemindersStore,
                                      fields_of, priority_name)
from jornada.sync.tasks.filters import CompletedFilter
from jornada.webapi.applescript import fake_runner

EASTERN = timezone(timedelta(hours=-4))
UTC = timezone.utc
RID = "x-apple-reminder://ABCD-1234"


def entry(**overrides):
    base = {"id": RID, "name": "Buy milk", "body": "2%", "dueDate": "2026-09-10T04:00:00.000Z", "completed": False,
            "completionDate": "", "priority": 1, "modified": "2026-09-06T10:00:00.000Z"}
    return {**base, **overrides}


def test_priority_scale_maps_both_ways():
    assert [priority_name(n) for n in (0, 1, 4, 5, 6, 9, None, True, "x")] == [
        "normal", "high", "high", "normal", "low", "low", "normal", "normal", "normal"]
    assert fields_of(Task("a", priority="high"))["priority"] == 1
    assert fields_of(Task("a", priority="low"))["priority"] == 9
    assert fields_of(Task("a"))["priority"] == 0


def test_list_maps_reminders_to_tasks_in_the_local_zone():
    calls = []
    answers = [[entry(), entry(id="r2", name="Done", body="", dueDate="", completed=True,
                             completionDate="2026-09-08T04:00:00.000Z", priority=5, modified=""),
                entry(id="r3", name="Old", dueDate="2026-09-11T03:59:00.000Z", completed=True, completionDate="",
                      priority=7)]]
    store = AppleRemindersStore(list_name="Handheld", runner=fake_runner(answers, calls), zone=EASTERN)
    first, second, third = store.list()
    argv, stdin, _timeout = calls[0]
    assert argv[:4] == ("/usr/bin/osascript", "-l", "JavaScript", "-") and argv[4:] == ("Handheld",)
    assert stdin == LIST_SCRIPT and "run(argv)" in stdin
    assert first.id == RID and first.version == "2026-09-06T10:00:00.000Z"
    assert first.record == Task("Buy milk", due=date(2026, 9, 10), priority="high", notes="2%", uid=RID)
    assert second.record == Task("Done", completed=date(2026, 9, 8), priority="normal", uid="r2")
    assert second.version is None
    assert third.record.completed == UNKNOWN_COMPLETION_DATE and third.record.priority == "low"
    assert third.record.due == date(2026, 9, 10)     # 03:59Z on the 11th is still the 10th in Eastern time
    assert store.name == "apple-reminders" and store.list_name == "Handheld"


def test_create_update_delete_pass_the_task_as_one_json_argument():
    calls = []
    runner = fake_runner([{"id": RID, "modified": "m1"}, {"id": RID, "modified": "m2"}, {"id": RID, "deleted": True}], calls)
    store = AppleRemindersStore(runner=runner, zone=UTC)
    task = Task("Buy milk", due=date(2026, 9, 10), completed=date(2026, 9, 8), priority="high", notes="2%",
                categories=("ignored",))
    assert store.create(task) == RID
    argv, stdin, _ = calls[0]
    assert stdin == CREATE_SCRIPT and argv[4] == "Jornada"
    assert json.loads(argv[5]) == {"name": "Buy milk", "body": "2%", "due": "2026-09-10", "priority": 1,
                                   "completed": True, "completionDate": "2026-09-08"}
    assert store.update(RID, Task(" ", completed=UNKNOWN_COMPLETION_DATE)) == "m2"
    argv, stdin, _ = calls[1]
    assert stdin == UPDATE_SCRIPT and argv[4:6] == ("Jornada", RID)
    assert json.loads(argv[6]) == {"name": "(no subject)", "body": "", "due": "", "priority": 0,
                                   "completed": True, "completionDate": ""}
    store.delete(RID)
    assert calls[2][1] == DELETE_SCRIPT and calls[2][0][4:] == ("Jornada", RID)
    for script in (LIST_SCRIPT, CREATE_SCRIPT, UPDATE_SCRIPT, DELETE_SCRIPT):
        assert "JSON.stringify" in script and "Buy milk" not in script and "Jornada" not in script


def test_failures_become_store_errors():
    runner = fake_runner([RuntimeError("Reminders got an error: not allowed"), {"nope": 1}, [{"name": "no id"}],
                          {"id": ""}])
    store = AppleRemindersStore(runner=runner)
    with pytest.raises(StoreError) as info:
        store.list()
    assert "not allowed" in str(info.value) and "Apple Reminders" in str(info.value)
    with pytest.raises(StoreError):
        store.list()                       # an object instead of a list
    with pytest.raises(StoreError):
        store.list()                       # an entry without an id
    with pytest.raises(StoreError):
        store.create(Task("x"))            # no id for the new reminder
    with pytest.raises(StoreError):
        store.update("", Task("x"))
    with pytest.raises(StoreError):
        store.delete("  ")


def test_deleting_a_reminder_that_is_already_gone_is_only_logged():
    lines = []
    store = AppleRemindersStore(runner=fake_runner([{"id": RID, "deleted": False}]), log=lines.append)
    store.delete(RID)
    assert lines == [f"reminder {RID} was already gone from Apple Reminders"]


def test_backend_build_reads_settings_and_applies_the_completed_filter(tmp_path):
    calls = []
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None,
                           runner=fake_runner([[entry(completed=True)], []], calls))
    hidden = apple.build(Account("t", "tasks", "apple", (("list", "Handheld"), ("completed", "hide"))), {}, context)
    assert isinstance(hidden, CompletedFilter) and hidden.list() == () and calls[0][0][4:] == ("Handheld",)
    plain = apple.build(Account("t", "tasks", "apple"), {}, context)
    assert isinstance(plain, AppleRemindersStore) and plain.list_name == "Jornada"
    assert apple.BACKEND.key == "apple" and apple.BACKEND.missing_settings(Account("t", "tasks", "apple"), {}) == ()
    assert [s.key for s in apple.BACKEND.settings] == ["list"] and apple.BACKEND.login is None
