from datetime import date
from pathlib import Path

import pytest

from jornada.pim.models import Task
from jornada.pim.tasks import UNKNOWN_COMPLETION_DATE
from jornada.sync.accounts import Account, AccountError
from jornada.sync.base import StoreError
from jornada.sync.tasks import markdown_format, todotxt, todotxt_format
from jornada.sync.tasks.filters import CompletedFilter
from jornada.sync.tasks.linetask import hash_id, parse_date, unique_id
from jornada.sync.tasks.todotxt import TodoFileStore, read_lines, write_lines
from jornada.sync.registry import BuildContext

TODAY = date(2026, 9, 6)
LINE = "x 2026-09-08 2026-09-07 (A) Buy milk +project @home due:2026-09-10 id:k7d2"


def ids(*values):
    queue = list(values)
    return lambda: queue.pop(0)


# -- todo.txt grammar ---------------------------------------------------------------------

def test_parse_every_token_of_a_completed_line():
    parsed = todotxt_format.parse_line(LINE)
    assert parsed.task == Task("Buy milk", due=date(2026, 9, 10), start=date(2026, 9, 7), completed=date(2026, 9, 8),
                               priority="high", categories=("project", "home"))
    assert (parsed.explicit_id, parsed.letter, parsed.contexts, parsed.extras) == ("k7d2", "A", ("home",), ())
    assert todotxt_format.render_line(parsed.task, "k7d2", parsed, TODAY) == LINE


def test_parse_open_lines_priorities_and_kept_tags():
    parsed = todotxt_format.parse_line("(C) 2026-09-07 Meeting 10:30 with Bob @phone t:2026-09-09 http://x.y/z")
    assert parsed.task == Task("Meeting 10:30 with Bob http://x.y/z", start=date(2026, 9, 7), categories=("phone",))
    assert parsed.extras == (("t", "2026-09-09"),) and parsed.letter == "C" and parsed.explicit_id is None
    assert todotxt_format.parse_line("x Buy").task.completed == UNKNOWN_COMPLETION_DATE
    assert todotxt_format.parse_line("x (B) 2026-09-08 Buy").task == Task("Buy", completed=date(2026, 9, 8), priority="high")
    assert todotxt_format.parse_line("x 2026-09-08 Buy pri:D").task.priority == "low"
    assert todotxt_format.parse_line("2026-99-99 not a date").task == Task("2026-99-99 not a date")
    assert todotxt_format.parse_line("   ") is None and todotxt_format.parse_line("").__class__ is type(None)
    for letter, expected in (("A", "high"), ("B", "high"), ("C", "normal"), ("D", "low"), ("Z", "low"), (None, "normal")):
        assert todotxt_format.priority_of_letter(letter) == expected
    assert todotxt_format.letter_for("high", "B") == "B" and todotxt_format.letter_for("high", None) == "A"
    assert todotxt_format.letter_for("low", "Z") == "Z" and todotxt_format.letter_for("low", "A") == "D"
    assert todotxt_format.letter_for("normal", "C") == "C" and todotxt_format.letter_for("normal", "A") is None


def test_render_keeps_sigils_extras_and_letter_and_tokenizes_categories():
    previous = todotxt_format.parse_line("(B) Buy milk @home key:value id:x1")
    task = Task("Buy oat milk", due=date(2026, 9, 10), priority="high", categories=("home", "Follow up"))
    assert todotxt_format.render_line(task, "x1", previous, TODAY) == "(B) Buy oat milk @home +Follow_up due:2026-09-10 key:value id:x1"
    assert todotxt_format.render_line(Task("", completed=UNKNOWN_COMPLETION_DATE), "n1", None, TODAY) == "x 2026-09-06 (no subject) id:n1"
    assert todotxt_format.render_line(Task("Low", priority="low", start=date(2026, 9, 1)), "n2", None, TODAY) == "(D) 2026-09-01 Low id:n2"
    assert todotxt_format.add_id("Buy milk  ", "n3") == "Buy milk id:n3"


# -- Markdown grammar -----------------------------------------------------------------------

def test_markdown_parse_and_render():
    line = "  - [ ] Buy milk #errands ⏫ 🔁 every week 🛫 2026-09-01 📅 2026-09-10 🆔 k7d2"
    parsed = markdown_format.parse_line(line)
    assert parsed.task == Task("Buy milk", due=date(2026, 9, 10), start=date(2026, 9, 1), priority="high", categories=("errands",))
    assert (parsed.explicit_id, parsed.indent, parsed.bullet, parsed.tail) == ("k7d2", "  ", "-", ("🔁 every week",))
    assert markdown_format.render_line(parsed.task, "k7d2", parsed, TODAY) == line
    done = markdown_format.parse_line("* [x] Done thing 🔽 ✅ 2026-09-08")
    assert done.task == Task("Done thing", completed=date(2026, 9, 8), priority="low") and done.bullet == "*"
    assert markdown_format.parse_line("1. [X] Old").task.completed == UNKNOWN_COMPLETION_DATE
    assert markdown_format.parse_line("- [ ] Stale ✅ 2026-09-08").task.completed is None   # the box wins
    assert markdown_format.parse_line("- [ ] Oops 📅 soon").task.due is None
    for text in ("# Heading", "plain paragraph", "- bullet without a box", "- [-] cancelled", ""):
        assert markdown_format.parse_line(text) is None
    assert markdown_format.render_line(Task("New", categories=("a b",)), "n1", None, TODAY) == "- [ ] New #a_b 🆔 n1"
    assert markdown_format.render_line(Task("", completed=UNKNOWN_COMPLETION_DATE), "n2", None, TODAY) == "- [x] (no subject) ✅ 2026-09-06 🆔 n2"
    assert markdown_format.add_id("- [ ] x", "n3") == "- [ ] x 🆔 n3"


# -- identity --------------------------------------------------------------------------------

def test_hash_ids_are_stable_and_collisions_are_deterministic():
    assert hash_id("Buy  milk ") == hash_id("Buy milk") and len(hash_id("x")) == 8
    taken = {hash_id("a")}
    assert unique_id(hash_id("a"), taken) != hash_id("a") and unique_id(hash_id("a"), taken) == unique_id(hash_id("a"), taken)
    assert unique_id("free", taken) == "free"


def test_listing_assigns_stable_ids_and_the_first_write_makes_them_explicit(tmp_path):
    path = tmp_path / "todo.txt"
    path.write_text("Buy milk due:2026-09-10\nBuy milk due:2026-09-10\n\n(A) Call mum @phone\n", encoding="utf-8")
    store = TodoFileStore(path, new_id=ids("new1"), today=lambda: TODAY)
    first = store.list()
    assert [i.record.summary for i in first] == ["Buy milk", "Buy milk", "Call mum"]
    assert len({i.id for i in first}) == 3 and first[0].id == hash_id("Buy milk due:2026-09-10")
    assert [i.id for i in store.list()] == [i.id for i in first]
    assert store.create(Task("Write report", due=date(2026, 9, 12))) == "new1"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [f"Buy milk due:2026-09-10 id:{first[0].id}", f"Buy milk due:2026-09-10 id:{first[1].id}", "",
                     f"(A) Call mum @phone id:{first[2].id}", "Write report due:2026-09-12 id:new1"]
    assert [i.id for i in store.list()] == [i.id for i in first] + ["new1"]
    path.write_text(path.read_text(encoding="utf-8").replace("Call mum", "Call mum and dad"), encoding="utf-8")
    assert [i.id for i in store.list()] == [i.id for i in first] + ["new1"]     # an explicit id survives edits
    assert store.list()[2].record.summary == "Call mum and dad"


def test_update_and_delete_rewrite_only_their_line(tmp_path):
    path = tmp_path / "todo.txt"
    path.write_text("(B) Buy milk @home id:a1\n\nCall mum id:b2\n", encoding="utf-8")
    store = TodoFileStore(path, today=lambda: TODAY)
    store.update("a1", Task("Buy milk", completed=date(2026, 9, 8), priority="high", categories=("home",)))
    assert path.read_text(encoding="utf-8") == "x 2026-09-08 (B) Buy milk @home id:a1\n\nCall mum id:b2\n"
    store.delete("b2")
    assert path.read_text(encoding="utf-8") == "x 2026-09-08 (B) Buy milk @home id:a1\n\n"
    with pytest.raises(StoreError):
        store.update("nope", Task("x"))
    with pytest.raises(StoreError):
        store.delete("nope")


def test_done_path_splits_completed_tasks(tmp_path):
    path, done = tmp_path / "todo.txt", tmp_path / "done.txt"
    path.write_text("Open one id:o1\n", encoding="utf-8")
    store = TodoFileStore(path, done_path=done, new_id=ids("c1"), today=lambda: TODAY)
    assert store.create(Task("Already done", completed=date(2026, 9, 1))) == "c1"
    assert done.read_text(encoding="utf-8") == "x 2026-09-01 Already done id:c1\n" and "c1" not in path.read_text()
    assert [(i.id, i.record.is_completed) for i in store.list()] == [("o1", False), ("c1", True)]
    store.update("o1", Task("Open one", completed=UNKNOWN_COMPLETION_DATE))
    assert path.read_text(encoding="utf-8") == "" and done.read_text(encoding="utf-8").splitlines()[-1] == "x 2026-09-06 Open one id:o1"
    store.update("c1", Task("Already done"))
    assert path.read_text(encoding="utf-8") == "Already done id:c1\n" and "c1" not in done.read_text(encoding="utf-8")
    assert store.done_path == done and store.path == path


def test_markdown_mode_preserves_everything_around_the_tasks(tmp_path):
    path = tmp_path / "Tasks.md"
    text = "# Week 37\n\nSome notes.\n\n- [ ] Buy milk 📅 2026-09-10\n\t- [x] Nested done ✅ 2026-09-05\n- plain bullet\n"
    path.write_text(text, encoding="utf-8")
    store = TodoFileStore(path, markdown_format.FORMAT, new_id=ids("m1"), today=lambda: TODAY)
    items = store.list()
    assert [(i.record.summary, i.record.due, i.record.is_completed) for i in items] == [
        ("Buy milk", date(2026, 9, 10), False), ("Nested done", None, True)]
    store.update(items[1].id, Task("Nested done", completed=date(2026, 9, 5), priority="high"))
    store.create(Task("Call mum", categories=("family",)))
    assert path.read_text(encoding="utf-8") == (
        f"# Week 37\n\nSome notes.\n\n- [ ] Buy milk 📅 2026-09-10 🆔 {items[0].id}\n"
        f"\t- [x] Nested done ⏫ ✅ 2026-09-05 🆔 {items[1].id}\n- plain bullet\n- [ ] Call mum #family 🆔 m1\n")


def test_writes_are_atomic_and_create_missing_files(tmp_path):
    path = tmp_path / "deep" / "todo.txt"
    store = TodoFileStore(path, new_id=ids("n1"))
    assert store.list() == ()
    store.create(Task("First"))
    assert path.read_text(encoding="utf-8") == "First id:n1\n" and list(path.parent.iterdir()) == [path]
    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"One id:a\r\nTwo id:b\r\n")
    TodoFileStore(crlf).delete("a")
    assert crlf.read_bytes() == b"Two id:b\r\n"
    assert read_lines(crlf) == (("Two id:b",), "\r\n") and read_lines(tmp_path / "missing") == ((), "\n")
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(StoreError):
        TodoFileStore(tmp_path / "bad.txt").list()
    with pytest.raises(StoreError):
        write_lines(tmp_path, ("x",))                     # a directory cannot be replaced
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_backend_build_reads_settings(tmp_path):
    context = BuildContext(log=lambda _line: None, sync_dir=tmp_path, save_secrets=lambda _c: None)
    account = Account("f", "tasks", "todotxt", (("path", str(tmp_path / "t.md")), ("format", "markdown"),
                                               ("done_path", str(tmp_path / "d.md")), ("completed", "hide")))
    store = todotxt.build(account, {}, context)
    assert isinstance(store, CompletedFilter) and isinstance(store.inner, TodoFileStore)
    assert store.inner.path == tmp_path / "t.md" and store.inner.done_path == tmp_path / "d.md"
    plain = todotxt.build(Account("f", "tasks", "todotxt", (("path", "~/todo.txt"),)), {}, context)
    assert isinstance(plain, TodoFileStore) and plain.path == Path("~/todo.txt").expanduser() and plain.done_path is None
    with pytest.raises(AccountError):
        todotxt.build(Account("f", "tasks", "todotxt"), {}, context)
    with pytest.raises(AccountError):
        todotxt.build(Account("f", "tasks", "todotxt", (("path", "x"), ("format", "org"))), {}, context)
    assert todotxt.BACKEND.missing_settings(Account("f", "tasks", "todotxt"), {}) == ("path",)
    assert [s.key for s in todotxt.BACKEND.settings] == ["path", "format", "done_path"]


def test_new_lines_get_the_hash_of_their_own_text_as_id(tmp_path):
    task = Task("Write report", due=date(2026, 9, 12), priority="high", categories=("work",))
    store = TodoFileStore(tmp_path / "todo.txt", today=lambda: TODAY)
    first = store.create(task)
    assert first == hash_id(todotxt_format.render_line(task, "", None, TODAY)) and len(first) == 8
    assert TodoFileStore(tmp_path / "other.txt", today=lambda: TODAY).create(task) == first     # deterministic
    second = store.create(task)                                                                 # the same text again
    assert second != first and second == unique_id(first, {first})
    assert [i.id for i in store.list()] == [first, second]
    assert (tmp_path / "todo.txt").read_text(encoding="utf-8") == (
        f"(A) Write report +work due:2026-09-12 id:{first}\n(A) Write report +work due:2026-09-12 id:{second}\n")
    markdown = TodoFileStore(tmp_path / "t.md", markdown_format.FORMAT, today=lambda: TODAY)
    assert markdown.create(task) == hash_id(markdown_format.render_line(task, "", None, TODAY))


def test_dates_must_look_like_yyyy_mm_dd():
    assert parse_date("2026-09-10") == date(2026, 9, 10)
    for text in ("2026-W36-7", "20260910", "2026-09-31", "2026-9-10", "", None, "2026-09-10T00:00"):
        assert parse_date(text) is None


def test_file_errors_are_reported_and_leave_no_temp_files(tmp_path):
    with pytest.raises(StoreError) as info:
        read_lines(tmp_path)                                    # a directory is not a file
    assert "cannot read" in str(info.value)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(StoreError) as info:
        write_lines(blocker / "todo.txt", ("x",))               # the parent is a file
    assert "cannot write" in str(info.value)
    with pytest.raises(TypeError):
        write_lines(tmp_path / "todo.txt", (1,))                # not text: the temp file is discarded
    assert not (tmp_path / "todo.txt").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
